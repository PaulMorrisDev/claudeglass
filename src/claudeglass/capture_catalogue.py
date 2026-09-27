"""Metrics capture: the words Claude may write, and the metrics behind them.

Metrics capture is opt-in. While it is on, a small hook adds a short note
to each session and subagent start (see ``hooks/capture-hook.py``) asking
Claude to end its replies with a one-line tag, for example
``[tl: task=bugfix brief=partial level=normal]``. ClaudeGlass reads the tags
back out of the transcripts to explain what the work was, not only what it
cost.

This module is the single source of truth for what those tags may say.
Every value is a closed vocabulary: a word outside it is dropped by the
parser (``capture_tags.py``), so no free text Claude writes is ever stored.
It has no imports from the rest of the package, so the parser, the hook
script's generated JSON and the dashboard all read the same lists.

It also holds every metric capture can switch on (:data:`METRICS`): what
each captures and why, the level it belongs to, the hook events it
needs, and the lines of the note that asks Claude for it
(:func:`note_text`). ``hooks/capture-catalogue.json`` is the part the
hook script needs (:func:`export_json`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: The marker every capture note carries, followed by the note format
#: version and the codes of the metrics it asks for
#: (``tl-cap v1 task,brief,level``). The parser finds capture notes by it.
NOTE_MARKER = "tl-cap v"

#: Current note format version.
NOTE_VERSION = 1

#: ``[tl: ...]`` keys (and the ``[result: ...]`` extras) -> the words each
#: may take. A key whose value is a comma list (``missing=files,goal``) is
#: in :data:`LIST_KEYS`.
TAG_VOCAB: dict[str, tuple[str, ...]] = {
    # Essentials, main session
    "task": (
        "feature",
        "bugfix",
        "refactor",
        "debug",
        "docs",
        "review",
        "test",
        "research",
        "plan",
        "ops",
        "chat",
    ),
    "brief": ("clear", "partial", "vague"),
    "level": ("easy", "normal", "hard"),
    "shift": ("new", "build", "grew", "redo", "fix"),
    # Standard, main session
    "size": ("xs", "s", "m", "l", "xl"),
    "missing": ("files", "goal", "constraints", "done", "repro", "scope", "none"),
    "plan": ("none", "made", "following", "deviated"),
    "skill": ("helped", "unneeded", "would-help", "none"),
    "found": ("yes", "partial", "no"),
    # Standard, subagent report (inside [result: ...])
    "fit": ("smaller", "right", "larger"),
    "rules": ("used", "unused"),
    # Deep, main session
    "prior": ("needed", "some", "none"),
    "detour": ("none", "dead-end", "reread", "overbuilt", "env", "flaky"),
    "check": ("targeted", "full", "build", "run", "manual", "none"),
    "out": ("needed", "part", "unneeded"),
    "useful": ("yes", "part", "no"),
}

#: Plain names for the ``task`` words, so "bugfix" reads "Bug fix"
#: wherever a kind of task is shown: every table with a task column
#: (``helptext.TASK_TABLES``), the brief templates' "why" line, and the
#: dashboard's "Kind of task" picker (``/api/profile-goals``). Lowercase
#: one in running text ("bug fix work").
TASK_LABELS: dict[str, str] = {
    "feature": "Feature",
    "bugfix": "Bug fix",
    "refactor": "Refactor",
    "debug": "Debugging",
    "docs": "Docs",
    "review": "Review",
    "test": "Tests",
    "research": "Research",
    "plan": "Planning",
    "ops": "Ops",
    "chat": "Chat",
}


def task_words(task: str) -> str:
    """``task``'s plain name for running text ("bug fix" for ``bugfix``);
    a word outside the vocabulary as it is."""
    return TASK_LABELS.get(task, task).lower()


#: Keys whose value is a comma-separated list of words.
LIST_KEYS = frozenset({"missing"})

#: ``[result: <word> ...]``: the subagent's own account of finishing.
RESULT_WORDS = ("done", "partial", "blocked")

#: ``[retry: <word>]`` at the start of a brief: why an agent is being run
#: again. ``scope`` means the task itself changed or was cut too wide.
RETRY_REASONS = ("model", "brief", "tools", "scope", "other")

#: ``[spawn: <word>]`` at the start of a brief: why the work was handed to
#: an agent at all.
SPAWN_REASONS = ("parallel", "isolate", "cheaper", "specialist", "review")

#: A skill name kept with ``skill=would-help:<name>``: the same shape as a
#: Claude Code skill or plugin skill name. Anything else is cut to a bare
#: ``would-help``, and a name that matches no skill in the transcript is
#: dropped too (see ``capture_tags.parse_reply_tags``).
SKILL_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"

#: The line Claude writes when ``feedback_reminder`` is on (below), word
#: for word. The note asks for it *before* the ``[tl: ...]`` tag (CAP-1),
#: and ``capture_tags`` strips it from a reply's tail before matching the
#: trailing tag, so it doesn't matter if Claude writes them the other
#: way round.
FEEDBACK_REMINDER_LINE = "Finished? Run /tl-feedback: a few ticks make your savings tips fit how you work."

#: How Claude sets a coaching tip or the reminder apart in its reply, so
#: it stands out from the work in the terminal and the desktop app alike:
#: after a blank line, a quote block opening with a sign and a bold
#: label. A warning sign for a coaching tip, a light bulb for the
#: reminder.
TIP_LABEL = "> ⚠️ **ClaudeGlass tip:**"
REMINDER_LABEL = "> 💡 **ClaudeGlass:**"
_TIP_ASK = f'a blank line and then a quote block starting "{TIP_LABEL}"'


# -- the metrics -----------------------------------------------------------

#: Capture levels, cheapest first. Each includes every metric of the
#: levels before it; ``custom`` is any other set (see :func:`level_of`).
LEVELS = ("off", "free", "essentials", "standard", "deep")
CUSTOM_LEVEL = "custom"

#: CAP-8: how long a fresh "off" -> "on" switch runs before switching
#: itself off again, when nothing says otherwise -- so capture never runs
#: forever unnoticed just because nobody thought to time-box it. Shared by
#: ``onboarding.ask_capture_until`` (the interactive/answers-file question)
#: and ``config.set_capture`` (the actual default, applied at every path
#: that can turn capture on: non-interactive ``init``, ``capture on``/
#: ``level``, and ``POST /api/capture``) -- defined here, rather than in
#: either of those, since ``onboarding`` imports from ``config`` and both
#: already depend on this module.
DEFAULT_CAPTURE_TIMEBOX_DAYS = 14

#: Display names for the levels, as the dashboard and CLI show them.
LEVEL_TITLES = {
    "off": "Off",
    "free": "Free",
    "essentials": "Essentials",
    "standard": "Standard",
    "deep": "Deep",
    "custom": "Custom",
}

#: What each level adds, for the init question and the Capture page.
LEVEL_SUMMARIES = {
    "off": "Nothing is captured and no tokens are used.",
    "free": "Local signals from hooks that log to a file. Uses no Claude tokens.",
    "essentials": "Claude tags each piece of work: what kind it was, how clear the request was, how hard, "
    "how big, and when the task changed. Subagents say whether they finished.",
    "standard": "Adds what the request lacked, planning, skills, research, "
    "and each subagent's view of its model, rules and brief.",
    "deep": "Adds how much earlier context was needed, how the change was checked, and a "
    "short rating after large tool outputs. Also turns on the /tl-feedback survey, its reminder note, "
    "and Claude's one-line reminder to run it when a piece of work is done.",
}

#: Suggestion themes a metric can feed (``Metric.powers``), in the order
#: the Work habits tab shows them.
THEMES = {
    "breakdown": "Breaking down work",
    "information": "Giving Claude information",
    "research": "Researching",
    "planning": "Planning",
    "skills": "Using skills",
    "delegation": "Delegating to agents",
    "tool_output": "Tool output",
    "verification": "Checking changes",
    "context": "Clearing context",
    "waiting": "Waiting and permissions",
    "outcome": "Cost per finished piece of work",
    "models": "Model and effort fit",
    "profiles": "Profiles per kind of task",
    "measuring": "Measuring your changes",
}

#: Where a metric is captured, in the order the Capture page groups them.
SECTIONS = {
    "main": "Main session",
    "subagents": "Subagents and briefs, at any depth",
    "skills_plans": "Skills and plans",
    "tools": "Tool calls",
    "signals": "Free local signals",
    "derived": "Always measured",
    "coaching": "Live coaching",
    "feedback": "Your feedback",
}

#: Metric groups. ``free`` to ``deep`` are the capture levels; ``derived``
#: is always on and costs nothing; ``feedback`` and ``coaching`` are
#: switched on one by one at any level (switching into Deep turns the
#: :data:`DEEP_FEEDBACK_IDS` on too).
GROUPS = ("derived", "free", "essentials", "standard", "deep", "feedback", "coaching")

#: Metric groups that make up the levels, in level order.
LEVEL_GROUPS = ("free", "essentials", "standard", "deep")

#: SessionStart sources the note is added on. ``resume`` is left out: a
#: resumed session already carries the note from its start.
SESSION_START_MATCHER = "startup|clear|compact"

#: Built-in agents that do no user work (they set up Claude Code itself):
#: they get no note.
SKIP_AGENT_TYPES = ("statusline-setup", "output-style-setup")

#: Built-in agents that start without your CLAUDE.md files, so asking
#: whether they used its rules, or rating a brief they're given by
#: Claude Code itself, tells nothing.
NO_RULES_AGENT_TYPES = ("Explore", "Plan")

#: A tool result at least this many tokens long gets a ``big_output``
#: note (Deep). Measured as characters / 4.
BIG_OUTPUT_TOKENS = 8000

#: Tools whose results get a ``web`` note (Deep).
WEB_TOOLS = ("WebFetch", "WebSearch")

#: Tools whose results can be large enough for a ``big_output`` note
#: (Deep), as the PostToolUse matcher. Claude Code only reads a hook's
#: note when it waits for the hook, so this entry runs in the
#: foreground; matching only these tools keeps edits and agent calls
#: from waiting on it.
BIG_OUTPUT_TOOLS = ("Bash", "Read", "Grep", "Glob", *WEB_TOOLS, "mcp__.*")

#: Hook event -> the free signal it records. ``Stop`` and ``StopFailure``
#: both feed ``turn_signals`` (SIG-3): an independent, hook-level check
#: next to what the parser already derives from the transcript for a
#: turn's own outcome (``model.py``'s ``EventKind.API_ERROR``/``subkind``).
SIGNAL_EVENTS = {
    "SessionEnd": "session_end",
    "Notification": "waits",
    "PermissionRequest": "permissions",
    "Stop": "turn_signals",
    "StopFailure": "turn_signals",
}

#: Why a session ended, as SessionEnd reports it; anything else is
#: "other". ``bypass_permissions_disabled`` was removed in Claude Code
#: v2.1.234 (docs/en/hooks.md, curl-verified) -- it is kept here only so
#: an old signal line that still holds it reads back correctly; a
#: current SessionEnd never sends it again. ``resume`` (also
#: curl-verified) was missing outright (SIG-1).
SESSION_END_REASONS = ("clear", "resume", "logout", "prompt_input_exit", "bypass_permissions_disabled", "other")

#: What Claude waited for: a permission prompt, your next message, a
#: question it asked (an MCP elicitation), someone else's input in an
#: agent view or team, a claude.ai usage limit's auto-resume, or
#: something else (SIG-1; the full Notification type list is
#: curl-verified against docs/en/hooks.md).
WAIT_KINDS = ("permission", "idle", "question", "agent", "quota", "other")

#: How a turn ended, as the ``Stop`` hook reports it (SIG-3): normally, or
#: re-entrant (``stop_hook_active`` -- Claude Code already ran a Stop hook
#: for this turn and is asking again, usually because a hook blocked the
#: first attempt).
TURN_STATES = ("normal", "reentrant")

#: The ``StopFailure`` hook's ``error`` field (SIG-3, curl-verified
#: against docs/en/hooks.md): the closed set of API-error kinds Claude
#: Code itself distinguishes. Never its optional ``error_details`` or
#: ``last_assistant_message`` -- for ``StopFailure`` the latter holds the
#: raw API error string, so it never reaches a signal line.
STOP_FAILURE_ERRORS = (
    "rate_limit",
    "overloaded",
    "authentication_failed",
    "oauth_org_not_allowed",
    "account_on_hold",
    "billing_error",
    "invalid_request",
    "model_not_found",
    "server_error",
    "max_output_tokens",
    "cloud_credential_error",
    "unknown",
)

#: Folder under the data folder that holds the signal files, one per
#: month (``YYYY-MM.jsonl``).
SIGNALS_DIR = "signals"


# -- who writes the reply tags --------------------------------------------------

#: Who writes the main session's ``[tl: ...]`` tag (``[capture] tagger``):
#: Claude, at the end of its final reply to each message (the default), or
#: Claude Haiku, which the hook asks after each turn, in the background,
#: with a short excerpt of it (:func:`judge_text`). With ``haiku`` the
#: session note asks for no tag, so replies end as they would anyway and
#: the session carries no tag list; the words land in :data:`JUDGE_DIR`
#: instead. Subagent reports and brief markers are Claude's to write
#: either way.
TAGGERS = ("claude", "haiku")
DEFAULT_TAGGER = "claude"

#: What ``CaptureConfig.hook_metrics`` adds while Haiku writes the tags,
#: so the hooks installed include the ``Stop`` entry that asks it.
HAIKU_TAGGER_HOOK = "haiku_tagger"

#: The model the hook asks, as ``claude --model`` takes it.
JUDGE_MODEL = "haiku"

#: Folder under the data folder that holds Haiku's tags, one file per
#: month (``YYYY-MM.jsonl``): the time, the salted session hash, the id
#: of the reply tagged, the tag's words and what the call cost. Never the
#: excerpt or anything else Haiku wrote.
JUDGE_DIR = "tags"

#: Set for the ``claude -p`` call the hook makes, so the hook does
#: nothing should that call run it.
JUDGE_ENV = "CLAUDEGLASS_JUDGE"

#: Seconds the hook waits for Haiku before giving up on a turn.
JUDGE_TIMEOUT_S = 60

#: Haiku's thinking budget, in tokens (``MAX_THINKING_TOKENS``); 0 is
#: no thinking. ``scripts/eval-tagger.py`` measures what it changes.
JUDGE_THINKING_TOKENS = 0

#: About what one Haiku call costs, in USD, for estimates before any
#: has run: about 1,300 tokens read (Claude Code's own frame, the
#: instructions and the excerpt) and 25 written, at Haiku's list price.
JUDGE_USD_PER_CALL = 0.0015

#: What the excerpt Haiku reads holds at most, in characters: your
#: message, your message before it, the end of Claude's final reply and
#: each shell command; and how many commands.
JUDGE_LIMITS = {"prompt": 2000, "previous": 300, "reply": 1500, "command": 100, "commands": 6}

#: Why a turn got no Haiku tag, as its line in :data:`JUDGE_DIR` says:
#: no ``claude`` command on the hook's path, Haiku took too long, the call
#: failed, or its answer held no tag.
JUDGE_ERRORS = ("no_cli", "timeout", "failed", "no_tag")

JUDGE_INTRO = (
    "You label one exchange between a user and Claude, an AI coding assistant, for the user's own usage "
    "analytics. You get an excerpt of it: the user's message, what Claude did, and the end of Claude's final "
    "reply. Answer with one line and nothing else, [tl: key=word ...], using only these keys and words. In "
    'them, "you" means Claude:'
)
#: The last line of what Haiku is told, in place of the note's
#: ``SKIP_KEY_LINE``: it sees an excerpt, not the work.
JUDGE_RULE = (
    "Judge only from the excerpt: a plan, skill or check it doesn't show wasn't there. Leave out a key that "
    "doesn't fit this kind of work, or that you can't judge."
)

#: The hook script that adds capture notes, and the catalogue it reads,
#: installed side by side under ``<config-dir>/hooks/``.
HOOK_SCRIPT = "capture-hook.py"
CATALOGUE_FILE = "capture-catalogue.json"


# -- coaching notes ------------------------------------------------------------

#: The marker every coaching note (``coaching_notes``) carries, then its
#: format version and the hint's kind (``tl-coach v1 quiet_output``). The
#: parser finds coaching notes by it. It differs from :data:`NOTE_MARKER`
#: so a coaching note never makes a session count as captured, whose
#: replies are then expected to carry tags.
COACH_MARKER = "tl-coach v"
COACH_VERSION = 1

#: Your own split points and plan habit, worked out from your recent
#: sessions by the dashboard's service (``coaching.py``) for the hook to
#: read, in the data folder.
COACHING_FILE = "coaching.json"

#: What the hook keeps between calls, per session: which hint showed
#: when, and how far each subagent run had got. In the data folder.
COACH_STATE_FILE = "coach-state.json"

#: Tools whose results a coaching note may follow: every tool the
#: large-output note watches, and ``ExitPlanMode`` for an approved plan.
COACHING_TOOLS = (*BIG_OUTPUT_TOOLS, "ExitPlanMode")

#: Tools the ``explore_reads`` hint counts as reads and searches.
COACHING_READ_TOOLS = ("Read", "Grep", "Glob")

#: The live hints: after a tool result (the first four) and when you
#: send a message (the rest), most useful first when more than one
#: applies. ``repeat_ask`` to ``big_paste`` are about how you prompt.
COACHING_HINTS = (
    "plan_fresh",
    "split_run",
    "quiet_output",
    "explore_reads",
    "repeat_ask",
    "drip_feed",
    "stop_loop",
    "plan_first",
    "vague_fix",
    "big_paste",
    "cache_cold",
    "clear_context",
)

#: Phrases that mark a message as correcting Claude ("that's wrong",
#: "still broken", "why did you", "undo that"), matched case-blind in a
#: message's first :data:`CORRECTION_SCAN_CHARS` characters. The parser
#: keeps only the yes/no (``Turn.human_correction``); the hook uses it
#: for the prompting hints. A bare "no" is deliberately not a match:
#: "no, go ahead" is as common as a correction.
CORRECTION_PATTERN = (
    r"\b(?:"
    r"that'?s (?:wrong|not right|not what|incorrect|broken)"
    r"|th(?:is|at) (?:is|was) (?:wrong|broken|incorrect|not (?:right|working|what))"
    r"|it'?s (?:still )?(?:broken|wrong|not working|failing|incorrect)"
    r"|(?:still|it still) (?:broken|failing|wrong|not working|doesn'?t work|fails)"
    r"|(?:doesn'?t|does not|didn'?t|did not) work"
    r"|not what (?:i|we) (?:asked|wanted|meant|said)"
    r"|you (?:broke|missed|forgot|ignored|didn'?t (?:do|read|follow|check|run|fix))"
    r"|why (?:did|didn'?t|would|are|is) you"
    r"|(?:undo|revert|roll back) (?:that|this|it|the|your)"
    r"|that broke|you'?ve broken|try again|redo (?:it|that|this)"
    r"|wrong (?:file|approach|answer|place|branch|one)"
    r")\b"
)
CORRECTION_SCAN_CHARS = 200

#: Words that make a short message a request to fix something, on top of
#: :data:`CORRECTION_PATTERN`: "fix this", "still an error", "broken
#: again". Only ``vague_fix`` uses it; ``drip_feed`` goes by what Claude
#: did, not by your words.
FIX_PATTERN = r"\b(?:fix|fixed|broken|wrong|incorrect|still|again|bug|error|errors|failing|fails|crash(?:es|ed)?)\b"

#: Anything that makes a correction specific: a path, a file name, a
#: quote, code, a number, a line of an error, an image, or what it
#: should be instead ("it should say Hi", "make it red", "change it to
#: blue"). Not an apostrophe: "it's wrong" is as vague as "wrong"; nor
#: "it should work" or "make it right", which say nothing new.
SPECIFIC_PATTERN = (
    r"[`\"/\\:#<>(){}\[\]=]|\w\.\w|\d"
    r"|(?i:\b(?:should|must|needs? to|supposed to|meant to)\s+(?:say|show|read|print|return|display|be|use|have"
    r"|look|go)\s+(?!(?:working|fixed|right|correct|better|ok|okay|fine|done|good)\b)\w+"
    r"|\bmake (?:it|this|that|them) (?!(?:work|right|better|correct)\b)\w+"
    r"|\b(?:change|set|turn|rename)\b[^.!?]{0,40}\bto\b"
    r"|\b(?:expected|instead|rather than)\b)"
)

#: How Claude Code records that you stopped a reply (Esc).
INTERRUPT_PREFIX = "[Request interrupted"

#: A message that only acknowledges ("thanks", "ok", "looks good"): sent
#: after a change, it isn't another request.
_ACK_WORDS = r"thanks?|thank you|thx|ty|ok(?:ay)?|great|perfect|nice|cool|awesome|lgtm|looks good|all good|good|done|yes|yep|no"
ACK_PATTERN = rf"(?:{_ACK_WORDS})(?:[\s!.,]+(?:{_ACK_WORDS}))*[\s!.,]*"

#: A line of a list in a message: "1. ...", "2) ...", "- ...", "* ...".
LIST_ITEM_PATTERN = r"(?m)^[ \t]*(?:\d{1,2}[.)]|[-*\u2022])[ \t]+\S"

#: A sentence that asks for a change, and the items it lists ("Add login,
#: a settings page and an admin screen"): a verb that asks for a change
#: at the start of a sentence or after "and", "then", "also" or a comma.
ACTION_PATTERN = (
    r"(?:^|[.;:!?\n,]|\band\b|\bthen\b|\balso\b)\s*(?:please\s+)?(?:add|create|build|implement|make|move|"
    r"migrate|refactor|rename|remove|delete|update|change|replace|write|fix|set up|convert|integrate|split|merge|"
    r"extract|port|upgrade|wire up|introduce|drop|rewrite|redesign|clean up|support)\b"
)
#: What separates the items of one listing sentence.
ITEM_SEPARATOR_PATTERN = r",\s*and\b|,|;|\band\b"
#: Where one sentence ends: a full stop, "!" or "?" before a space or the
#: end (not the one in "page.html"), or a line break.
SENTENCE_END_PATTERN = r"[.!?]+(?=\s|$)|\n+"

#: Tools that change a file. A message Claude answered with one of these
#: asked for a change, whatever its words (``drip_feed``).
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

#: When each hint applies, and how often it may repeat. Each can be
#: changed in ``config.toml``'s ``[thresholds]`` as ``coaching_<key>``.
COACHING_THRESHOLDS = {
    #: Context, in tokens, at which a message you send gets the /clear hint.
    "clear_context_tokens": 100_000,
    #: The smallest context the expired-cache hint is worth mentioning.
    "cold_min_tokens": 20_000,
    #: A tool result this many tokens long gets the narrower-output hint.
    "quiet_output_tokens": 8_000,
    #: This many reads and searches for one message get the Explore hint.
    "explore_reads": 8,
    #: Planning context, in tokens, kept after an approved plan before the
    #: fresh-session hint applies (``plan_handoff_min_dropped_tokens``'s
    #: default).
    "plan_fresh_tokens": 40_000,
    #: This many small requests in a row, each of which Claude answered by
    #: changing files, get the plan-it-as-one-prompt hint...
    "drip_count": 3,
    #: ...when each was sent within this long of Claude's reply before it.
    "drip_window_minutes": 20,
    #: A message longer than this isn't a small request.
    "drip_chars": 300,
    #: A fix request this short, naming nothing specific, is vague.
    "vague_fix_chars": 80,
    #: A message this many tokens long gets the big-paste hint.
    "big_paste_tokens": 10_000,
    #: A request asking for this many separate changes, outside plan mode,
    #: gets the plan-first hint...
    "plan_steps": 4,
    #: ...when it's at least this long.
    "plan_min_chars": 150,
    #: A message sharing this much of its words with one you sent...
    "repeat_similarity": 0.8,
    #: ...within this long, is the same request again...
    "repeat_window_minutes": 60,
    #: ...when it has at least this many different words.
    "repeat_min_words": 4,
    #: Stopping Claude this many times...
    "stop_loop_count": 3,
    #: ...within this long gets the agree-the-approach hint.
    "stop_window_minutes": 20,
    #: A hint that showed stays quiet this long in the same session...
    "cooldown_minutes": 30,
    #: ...unless what's at stake has grown this many times since.
    "rearm_factor": 1.5,
}

#: What each hint asks of Claude. ``{placeholders}`` are filled from the
#: session: token counts in thousands (``150k``), an idle time, a count.
#: A note never carries a path, a command or your words.
COACHING_TEXT = {
    "cache_cold": (
        "The prompt cache expired while this session sat idle for {idle}, so this reply writes the whole context "
        "again: about {ctx} tokens at the cache-write price. If the user's message starts a task unrelated to the "
        "work so far, end your reply, before any tag, with " + _TIP_ASK + " saying so, and that /clear before a "
        "new task after a break avoids it. If it carries on the same work, don't mention it."
    ),
    "clear_context": (
        "This session's context is about {ctx} tokens, and every reply reads all of it again. If the user's "
        "message starts a task unrelated to the work so far, end your reply, before any tag, with " + _TIP_ASK
        + " saying that /clear before a new task would have saved that. If it carries on the same work, don't "
        "mention it."
    ),
    "drip_feed": (
        "The user has sent {count} small change requests in a row, one message each, and every message re-reads "
        "the whole context. Make this change, then end your reply, before any tag, with " + _TIP_ASK + " "
        "suggesting that working out everything the work still needs and sending it as one message gets it done "
        "in one pass, for fewer tokens."
    ),
    "repeat_ask": (
        "The user has sent much the same request again, so the last attempt probably missed what they wanted. "
        "Don't repeat the same approach: say in one line what you think went wrong, then try a different way, or "
        "ask one short question if you can't tell. End your reply, before any tag, with " + _TIP_ASK + " "
        "suggesting that saying what was wrong with the last attempt gets a better next one than sending the "
        "request again."
    ),
    "plan_first": (
        "The user's message asks for about {steps} separate changes, outside plan mode. Before changing anything, "
        "set out in a few lines how you'll go about it and in what order, then carry on unless they stop you. End "
        "that reply, before any tag, with " + _TIP_ASK + " suggesting plan mode (Shift+Tab) for a job this size: "
        "it agrees the approach before anything changes."
    ),
    "stop_loop": (
        "The user has stopped you {count} times in the last {minutes} minutes to change course. Before you change "
        "anything for this message, say in two or three lines what you'll do, and wait for a go-ahead if it's a "
        "large change. End that reply, before any tag, with " + _TIP_ASK + " saying that plan mode (Shift+Tab) "
        "agrees the approach before any work starts."
    ),
    "vague_fix": (
        "The user says something is wrong but not what they saw or expected. If the context doesn't make the "
        "problem clear, ask one short question (what they saw, what they expected, or the error text) before "
        "changing anything. If it does, fix it and end your reply, before any tag, with " + _TIP_ASK + " saying "
        "that naming what they saw and expected, or pasting the error, gets a fix first time."
    ),
    "big_paste": (
        "The user's message is about {tokens} tokens, and every later reply reads it again. If most of it is a "
        "log, a file or command output, end your reply, before any tag, with " + _TIP_ASK + " suggesting they "
        "paste only the part that matters, or save it to a file and give the path so only what's needed is read."
    ),
    "quiet_output": "That result was about {tokens} tokens, and every later reply reads it again. Next time, {how}.",
    "explore_reads": (
        "You've made {reads} reads and searches for this message, about {tokens} tokens that every later reply "
        "reads again. If more searching is needed, hand it to an Explore agent: it searches in its own context "
        "and sends back a short summary."
    ),
    "plan_fresh": (
        "This plan was approved with about {kept} tokens of planning in context, which every reply of the build "
        "reads again. Before you start building, tell the user in a quote block starting \"" + TIP_LABEL + "\" "
        "that building it in a fresh session (/clear, then ask Claude to carry out the saved plan) would carry "
        "about {kept} fewer tokens on each reply. Then carry on unless they stop you."
    ),
    "split_run": (
        "This run has made about {replies} replies, and every reply reads all of the run again. In this user's "
        "past sessions, {agent} runs cost less when split about every {every_n} replies. If more than a step or "
        "two is left, finish the step you're on and end your report with a short note of what's done, what's "
        "left and the files involved, so a fresh agent can carry on from it."
    ),
}

#: What the hook shows you itself, the moment you send the message, for
#: the prompting hints: Claude Code's hook ``systemMessage``, shown to
#: you and never sent to Claude, so it costs no tokens. Claude's reply
#: still ends with the tip, for an app that doesn't show hook messages.
#: Same ``{placeholders}`` as :data:`COACHING_TEXT`.
COACHING_NOTICE = {
    "drip_feed": "⚠️ ClaudeGlass: {count} small requests in a row, one message each. Work out everything that needs "
    "changing and send it as one prompt: it costs less.",
    "repeat_ask": "⚠️ ClaudeGlass: that's much the same request as before. Saying what was wrong with the last "
    "attempt helps more than sending it again.",
    "plan_first": "⚠️ ClaudeGlass: a {steps}-step request outside plan mode. Plan mode (Shift+Tab) agrees the "
    "approach before anything changes.",
    "stop_loop": "⚠️ ClaudeGlass: you've stopped Claude {count} times in {minutes} minutes. Plan mode (Shift+Tab) "
    "agrees the approach before work starts.",
    "vague_fix": "⚠️ ClaudeGlass: say what you saw and what you expected, or paste the error, to get a fix first "
    "time.",
    "big_paste": "⚠️ ClaudeGlass: this message is about {tokens} tokens, and every later reply reads it again. "
    "Paste only the part that matters, or give a file path.",
}

#: ``quiet_output``'s ``{how}``, by tool; ``""`` for any other tool.
COACHING_QUIET_HOW = {
    "Read": "read only the lines you need, with an offset and a limit",
    "Bash": "cut the command's output down first: a filter, head or tail, or a quieter flag",
    "Grep": "narrow the pattern or the path, or ask for file names or counts only",
    "Glob": "narrow the pattern",
    "": "ask for less: a narrower query or a smaller page",
}


@dataclass(frozen=True, slots=True)
class Metric:
    """One thing capture can measure, and everything the Capture page,
    the note hook and the cost estimates need to know about it."""

    id: str
    #: One of :data:`GROUPS`.
    group: str
    #: One of :data:`SECTIONS`.
    section: str
    #: Short name for the Capture page.
    title: str
    #: What it captures, in plain words.
    what: str
    #: Why it's worth capturing: what it lets ClaudeGlass tell you.
    why: str
    #: :data:`THEMES` it feeds.
    powers: tuple[str, ...] = ()
    #: How Claude writes it, for the Capture page (empty when Claude
    #: writes nothing).
    tag: str = ""
    #: Hook events it needs in Claude Code's settings.json.
    hooks: tuple[str, ...] = ()
    #: The line explaining its key in the main session's ``[tl: ...]``
    #: tag, and in a subagent's ``[result: ...]`` tag.
    main_line: str = ""
    sub_line: str = ""
    #: A line of its own in the main or subagent note.
    main_extra: str = ""
    sub_extra: str = ""
    #: Put ``main_extra`` before the ``[tl: ...]`` tag block instead of
    #: after it (CAP-1): for an extra that itself tells Claude to end its
    #: reply with something, which would otherwise compete with the tag
    #: instruction for "the last thing in the reply".
    extra_before_tag: bool = False
    #: ``main_extra`` for a note that asks for no tag (Haiku writes the
    #: tags), when its wording mentions the tag; empty to use ``main_extra``.
    main_extra_untagged: str = ""
    #: The note a PostToolUse hook adds after a matching tool result.
    tool_note: str = ""
    #: Other metrics it can't work without (a subagent's extras ride on
    #: ``result``).
    requires: tuple[str, ...] = ()
    #: About how many characters Claude writes for it each time.
    out_chars: int = 0


_TASK_WORDS = "|".join(TAG_VOCAB["task"])

METRICS: tuple[Metric, ...] = (
    # -- Essentials -------------------------------------------------------
    Metric(
        id="task",
        group="essentials",
        section="main",
        title="Kind of task",
        what="What kind of work each of your messages asked for: feature, bugfix, refactor, debug, docs, "
        "review, test, research, plan, ops or chat.",
        why="Cost per kind of task, and a profile tuned to each kind. Replaces ClaudeGlass's guess from the "
        "session's shape.",
        powers=("profiles", "outcome", "models", "measuring"),
        tag="task=" + _TASK_WORDS,
        hooks=("SessionStart",),
        main_line=f"task: {_TASK_WORDS} (the kind of work asked for)",
        out_chars=13,
    ),
    Metric(
        id="brief",
        group="essentials",
        section="main",
        title="How clear the request was",
        what="Whether your request was clear, partly clear or vague.",
        why="What vague requests cost you in extra turns, and how to brief Claude better.",
        powers=("information",),
        tag="brief=clear|partial|vague",
        hooks=("SessionStart",),
        main_line="brief: clear|partial|vague (how complete the request was)",
        out_chars=13,
    ),
    Metric(
        id="level",
        group="essentials",
        section="main",
        title="How hard the work was",
        what="Whether the work was easy, normal or hard.",
        why="Whether your model and effort fit the work: a lighter setup for easy work, and no cheaper-model "
        "suggestion for hard work.",
        powers=("models", "profiles", "planning", "measuring"),
        tag="level=easy|normal|hard",
        hooks=("SessionStart",),
        main_line="level: easy|normal|hard (how hard the work was)",
        out_chars=12,
    ),
    Metric(
        id="shift",
        group="essentials",
        section="main",
        title="Task changes",
        what="When the work changed: a new unrelated task, building on the last one, the scope growing, "
        "redoing earlier work, or fixing a fault in earlier work.",
        why="Task switching, scope creep, rework and fixes, and when a fresh session or plan mode would have "
        "been cheaper.",
        powers=("breakdown", "context", "planning"),
        tag="shift=new|build|grew|redo|fix",
        hooks=("SessionStart",),
        main_line="shift: new|build|grew|redo|fix, only if it applies (a new unrelated task; building on the "
        "last one; the scope grew; redoing earlier work; fixing a fault in it)",
        out_chars=5,
    ),
    Metric(
        id="size",
        group="essentials",
        section="main",
        title="Size of the work",
        what="How big each piece of work was, from xs to xl.",
        why="How you break work down: big asks that end in compaction or rework, and tiny asks that each "
        "pay the start-up cost. With the kind and difficulty of the work, it lets a change be judged on "
        "like-for-like work before and after it.",
        powers=("breakdown", "measuring"),
        tag="size=xs|s|m|l|xl",
        hooks=("SessionStart",),
        main_line="size: xs|s|m|l|xl (how big the work was)",
        out_chars=7,
    ),
    Metric(
        id="result",
        group="essentials",
        section="subagents",
        title="Did the agent finish",
        what="Each subagent's own account of whether it finished its task, finished part of it, or was "
        "blocked.",
        why="Which agents and models deliver, and which get re-run.",
        powers=("delegation", "models", "outcome"),
        tag="[result: done|partial|blocked]",
        hooks=("SubagentStart",),
        sub_line="done: you finished the task; partial: some of it; blocked: you could not go on",
        out_chars=15,
    ),
    Metric(
        id="retry",
        group="essentials",
        section="subagents",
        title="Why an agent was run again",
        what="When Claude starts an agent again because its last run fell short, the reason: model, brief, "
        "tools, scope or other.",
        why="Why agents are re-run, and a guard that stops ClaudeGlass suggesting a cheaper model for work "
        "that needed a stronger one.",
        powers=("delegation", "models"),
        tag="[retry: model|brief|tools|scope|other]",
        hooks=("SessionStart", "SubagentStart"),
        main_extra="When you start an agent again because its last run fell short, begin the brief with "
        "[retry: model|brief|tools|scope|other].",
        sub_extra="Re-running an agent? Begin its brief with [retry: model|brief|tools|scope|other].",
        out_chars=2,
    ),
    # -- Standard ---------------------------------------------------------
    Metric(
        id="missing",
        group="standard",
        section="main",
        title="What the request lacked",
        what="What your request left Claude to find or guess: files, goal, constraints, what done means, "
        "how to reproduce, scope, or none.",
        why="What to put in your prompts, or once in CLAUDE.md, so Claude stops searching for it.",
        powers=("information", "research"),
        tag="missing=files,goal,constraints,done,repro,scope|none",
        hooks=("SessionStart",),
        main_line="missing: files,goal,constraints,done,repro,scope or none (what the request lacked that you "
        "had to find or guess; a comma list)",
        out_chars=14,
    ),
    Metric(
        id="plan",
        group="standard",
        section="skills_plans",
        title="Planning",
        what="Whether the work had no plan, a plan was made, a plan was followed, or the work departed from "
        "it.",
        why="How accurate plans are, and whether planning first saves rework on hard tasks.",
        powers=("planning",),
        tag="plan=none|made|following|deviated",
        hooks=("SessionStart",),
        main_line="plan: none|made|following|deviated (no plan; you wrote one; you worked to one; you departed "
        "from it)",
        out_chars=10,
    ),
    Metric(
        id="skill",
        group="standard",
        section="skills_plans",
        title="Skills",
        what="Whether a skill helped, wasn't needed, or one of your listed skills would have helped (and "
        "which).",
        why="When to invoke a skill, skills that don't pay for their context, and skills you forget to use.",
        powers=("skills",),
        tag="skill=helped|unneeded|would-help[:name]|none",
        hooks=("SessionStart",),
        main_line="skill: helped|unneeded|would-help|none (whether a skill you ran helped; would-help:<name> "
        "if one of the listed skills would have)",
        out_chars=11,
    ),
    Metric(
        id="found",
        group="standard",
        section="main",
        title="Research result",
        what="For research and search work, whether Claude found what was asked.",
        why="How you research: when to give Claude pointers, and when an Explore agent is cheaper.",
        powers=("research",),
        tag="found=yes|partial|no",
        hooks=("SessionStart",),
        main_line="found: yes|partial|no (for research or search work: whether you found what was asked)",
        out_chars=6,
    ),
    Metric(
        id="fit",
        group="standard",
        section="subagents",
        title="Agent model fit",
        what="Each subagent's view of whether a smaller model would have done its task, or it needed a "
        "larger one.",
        why="Agent model tuning. Used only to rule a cheaper model out, never to recommend one.",
        powers=("models", "delegation"),
        tag="fit=smaller|right|larger",
        hooks=("SubagentStart",),
        sub_line="fit: smaller|right|larger (could a smaller model have done this task, or did it need a "
        "larger one)",
        requires=("result",),
        out_chars=10,
    ),
    Metric(
        id="rules",
        group="standard",
        section="subagents",
        title="Agent used your rules",
        what="Whether each subagent used the CLAUDE.md and memory instructions it was given.",
        why="Which agents could start without your CLAUDE.md files, saving their start-up tokens.",
        powers=("delegation",),
        tag="rules=used|unused",
        hooks=("SubagentStart",),
        sub_line="rules: used|unused (did you use the CLAUDE.md or memory instructions you were given)",
        requires=("result",),
        out_chars=10,
    ),
    Metric(
        id="agent_brief",
        group="standard",
        section="subagents",
        title="Agent brief quality",
        what="Each subagent's view of how complete its brief was, and what it lacked: files, goal, scope or "
        "what done means.",
        why="Brief quality per agent type, and better agent definitions and brief templates.",
        powers=("delegation", "information"),
        tag="brief=clear|partial|vague missing=files,goal,scope,done|none",
        hooks=("SubagentStart",),
        sub_line="brief: clear|partial|vague (how complete your brief was); missing: files,goal,scope,done or "
        "none (what it lacked)",
        requires=("result",),
        out_chars=24,
    ),
    # -- Deep -------------------------------------------------------------
    Metric(
        id="prior",
        group="deep",
        section="main",
        title="Earlier context needed",
        what="How much of the earlier conversation each reply needed.",
        why="A direct measure of when /clear was safe, and what it would have saved.",
        powers=("context",),
        tag="prior=needed|some|none",
        hooks=("SessionStart",),
        main_line="prior: needed|some|none (how much of the earlier conversation this work needed)",
        out_chars=10,
    ),
    Metric(
        id="check",
        group="deep",
        section="main",
        title="How changes were checked",
        what="How a change was verified: targeted tests, the full suite, a build, running it, by hand, or "
        "not at all.",
        why="Unchecked changes that later needed redoing or fixing, and full-suite output carried in context.",
        powers=("verification",),
        tag="check=targeted|full|build|run|manual|none",
        hooks=("SessionStart",),
        main_line="check: targeted|full|build|run|manual|none (how you verified the change)",
        out_chars=12,
    ),
    Metric(
        id="big_output",
        group="deep",
        section="tools",
        title="Large tool outputs",
        what=f"After a tool result of about {BIG_OUTPUT_TOKENS:,} tokens or more, how much of it Claude "
        "needed: all, part or none. Claude Code waits for the hook after each shell, read, search, web or "
        "MCP result. 'claudeglass capture status' shows how long that has added, measured from your own sessions.",
        why="Quieter commands, offset reads and output caps where big outputs weren't needed.",
        powers=("tool_output",),
        tag="out=needed|part|unneeded",
        hooks=("PostToolUse",),
        tool_note="That tool result was large. Add out=needed|part|unneeded (how much of it you needed) to "
        "the tag that ends your final reply.",
        out_chars=9,
    ),
    # -- Free local signals ----------------------------------------------
    Metric(
        id="session_end",
        group="free",
        section="signals",
        title="Why sessions end",
        what="Why each session ended: /clear, exit, logout or other.",
        why="Session length and batching tips, alongside task changes.",
        powers=("breakdown", "context"),
        hooks=("SessionEnd",),
    ),
    Metric(
        id="waits",
        group="free",
        section="signals",
        title="Waiting on you",
        what="When Claude waited for your permission or input. The transcript shows how long until you "
        "answered.",
        why="How often Claude sat waiting, and allow rules for routine commands.",
        powers=("waiting",),
        hooks=("Notification",),
    ),
    Metric(
        id="permissions",
        group="free",
        section="signals",
        title="Permission decisions",
        what="Each permission prompt: the tool name, never its arguments. The transcript shows what you "
        "decided.",
        why="Denials that led to rework, and allowlist suggestions.",
        powers=("waiting",),
        hooks=("PermissionRequest",),
    ),
    Metric(
        id="turn_signals",
        group="free",
        section="signals",
        title="How turns end",
        what="Whether each turn ended normally or Claude Code asked the Stop hook again. On a failed turn, "
        "the kind of API error, such as a rate limit or overload, never the error's own text.",
        why="An independent, hook-level check next to what the transcript already shows about limit hits "
        "and API errors.",
        powers=("waiting", "outcome"),
        hooks=("Stop", "StopFailure"),
    ),
    # -- Always measured --------------------------------------------------
    # The transcripts already record these, so they need no hook: the
    # nested CLAUDE.md and rules files that load, the commands and skills
    # you run, task lists, and API errors.
    Metric(
        id="instructions_loaded",
        group="derived",
        section="derived",
        title="Instruction files loaded",
        what="Instruction files that load during a session (nested CLAUDE.md and rules): kind and size.",
        why="What nested CLAUDE.md and rules files cost as they load.",
        powers=("information",),
    ),
    Metric(
        id="prompt_expansion",
        group="derived",
        section="skills_plans",
        title="Commands and skills you ran",
        what="Slash commands and skills you ran: name, what they added to context, and when in the session.",
        why="When you invoke skills, and what they add to context.",
        powers=("skills",),
    ),
    Metric(
        id="tasks",
        group="derived",
        section="derived",
        title="Task lists",
        what="How many tasks Claude created and completed.",
        why="How work is split into tasks, and how many get finished.",
        powers=("breakdown",),
    ),
    Metric(
        id="stop_failure",
        group="derived",
        section="derived",
        title="API errors",
        what="The kind of each API error that stopped a turn.",
        why="Turns lost to errors.",
        powers=("outcome",),
    ),
    Metric(
        id="prompt_features",
        group="derived",
        section="derived",
        title="What your messages contain",
        what="Whether each message names a file, has a code block, an error or stack trace, a URL, "
        "done-criteria wording or numbered steps. Also whether it's short. Only yes/no is kept.",
        why="How you give Claude information, measured without asking Claude: cost per task with and "
        "without file paths or errors.",
        powers=("information",),
    ),
    Metric(
        id="brief_features",
        group="derived",
        section="derived",
        title="What agent briefs contain",
        what="The same checks for the briefs Claude writes for agents, and whether a short report was asked "
        "for.",
        why="Brief quality per agent type, and long reports that weren't capped.",
        powers=("delegation", "information"),
    ),
    Metric(
        id="plan_features",
        group="derived",
        section="skills_plans",
        title="Plans",
        what="Each plan you approved or rejected: steps, files named and length.",
        why="Plan size against what the work then cost.",
        powers=("planning",),
    ),
    Metric(
        id="skill_timing",
        group="derived",
        section="skills_plans",
        title="Skill timing",
        what="How far into a piece of work a skill ran, and whether you or Claude started it.",
        why="Starting skills earlier, and skills Claude reaches for on its own.",
        powers=("skills",),
    ),
    Metric(
        id="spawn_tree",
        group="derived",
        section="derived",
        title="Agent chains",
        what="Cost per chain of agents and depth, and files an agent read that its parent had already read.",
        why="Flatter chains, and passing findings to agents instead of having them re-read.",
        powers=("delegation",),
    ),
    Metric(
        id="tool_loops",
        group="derived",
        section="derived",
        title="Repeated failures",
        what="The same command failing again and again in one piece of work.",
        why="Flaky tests and environment trouble that burn tokens.",
        powers=("verification", "tool_output"),
    ),
    Metric(
        id="research_split",
        group="derived",
        section="derived",
        title="Where research happens",
        what="Search and read tokens in the main session against those in Explore agents.",
        why="When to hand research to an agent.",
        powers=("research", "delegation"),
    ),
    # -- Live coaching ----------------------------------------------------
    Metric(
        id="coaching_line",
        group="coaching",
        section="coaching",
        title="Coaching line",
        what="A second status line with a live hint from your session. For example, a large context before "
        "a new task, a large last output, many reads so far, or small requests sent one at a time.",
        why="Advice where you work, at the moment it applies. The status line is never sent to Claude.",
        powers=("context", "tool_output", "research"),
    ),
    Metric(
        id="coaching_notes",
        group="coaching",
        section="coaching",
        title="Coaching notes from Claude",
        what="Live hints for where the status line doesn't show, such as the desktop app. When one applies, a "
        "hook adds a short note to Claude's context, and Claude acts on it or tells you in a highlighted tip: a large "
        "tool output, many reads for one message, a subagent run past the point where your own history says "
        "splitting pays, a plan approved on top of a lot of planning context, or a large context or an expired "
        "cache when you send a message. It also flags how you prompt: the same request again, a big task "
        "without a plan, small requests sent one at a time, a "
        "vague correction, a huge paste, or stopping Claude again and again.",
        why="Advice at the moment it applies, and Claude can often act on it itself. Each note costs a few dozen "
        "tokens for the rest of the session. Claude Code waits for the hook after each shell, read, search, web "
        "or MCP result and each message you send.",
        powers=("context", "tool_output", "research", "delegation", "planning"),
        hooks=("UserPromptSubmit", "PostToolUse"),
    ),
    Metric(
        id="brief_templates",
        group="coaching",
        section="coaching",
        title="Brief templates",
        what="Checklists per kind of task, built from what your own requests tend to lack, on "
        "Work habits to copy. Turned on, it also adds a /tl-brief skill you run with a request: Claude "
        "checks it against its checklist and asks once for anything missing.",
        why="Better first messages, so Claude spends less finding things out.",
        powers=("information",),
    ),
    # -- Feedback ---------------------------------------------------------
    Metric(
        id="feedback_skill",
        group="feedback",
        section="feedback",
        title="Feedback skill",
        what="A /tl-feedback skill you run after a piece of work. It asks four checkbox questions: the "
        "outcome, what slowed it, whether it was worth the tokens, and what would have helped. After an "
        "approved plan it asks a fifth: whether the build could have started fresh from the plan.",
        why="Cost per piece of work that met its goal, which outranks what Claude reports about itself. "
        "The plan answer tells the fresh-session tip and the suggested profile how you work.",
        powers=("outcome", "planning", "profiles"),
        tag="[tl-fb: outcome=… slow=… worth=… helped=… handoff=…]",
    ),
    Metric(
        id="feedback_note",
        group="feedback",
        section="feedback",
        title="Feedback reminder in the status line",
        what="A second status line reminding you to run /tl-feedback, and the same line on the dashboard "
        "banner.",
        why="A reminder that costs nothing: the status line is never sent to Claude.",
        powers=("outcome",),
    ),
    Metric(
        id="feedback_reminder",
        group="feedback",
        section="feedback",
        title="Feedback reminder from Claude",
        what="Claude adds a highlighted note suggesting /tl-feedback when it finishes a piece of work.",
        why="For people without the status line. Costs a few output tokens each time.",
        powers=("outcome",),
        hooks=("SessionStart",),
        main_extra="When you finish a piece of work the user asked for, add this before your tag, after a blank "
        f"line:\n{REMINDER_LABEL} {FEEDBACK_REMINDER_LINE}",
        extra_before_tag=True,
        main_extra_untagged="When you finish a piece of work the user asked for, end your reply with this, after a "
        f"blank line:\n{REMINDER_LABEL} {FEEDBACK_REMINDER_LINE}",
        out_chars=103,
    ),
    Metric(
        id="dashboard_rating",
        group="feedback",
        section="feedback",
        title="Rate sessions on the dashboard",
        what="The same checkboxes on Spend › Sessions, kept in ClaudeGlass's own store.",
        why="Feedback without spending tokens.",
        powers=("outcome",),
    ),
)

METRICS_BY_ID: dict[str, Metric] = {m.id: m for m in METRICS}

#: Metrics a capture level turns on (every group from ``free`` to ``deep``).
LEVEL_METRIC_IDS = tuple(m.id for m in METRICS if m.group in LEVEL_GROUPS)
#: Metrics switched on one by one (``[capture] feedback``/``coaching``).
FEEDBACK_IDS = tuple(m.id for m in METRICS if m.group == "feedback")
COACHING_IDS = tuple(m.id for m in METRICS if m.group == "coaching")
#: The feedback items a switch into Deep turns on as well
#: (``config.set_capture``): the /tl-feedback survey, its reminder note,
#: and Claude's one-line reminder to run it. Deep is the level for
#: someone who wants the fullest picture, and outcomes from the survey
#: outrank what Claude reports about itself. Leaving Deep keeps them;
#: ``capture feedback off`` takes them out. The dashboard rating stays a
#: choice of its own.
DEEP_FEEDBACK_IDS = ("feedback_skill", "feedback_note", "feedback_reminder")

#: CAP-5: metric ids retired from :data:`METRICS` (no longer asked, priced,
#: or shown), kept here only so a ``config.toml`` written before the
#: retirement still loads: ``_capture_list``'s config-file validation
#: allows them through, and ``with_requirements``/``active_metrics``
#: silently drop them (they are not in :data:`METRICS_BY_ID`) rather than
#: ever asking Claude for them again. Their words stay in
#: :data:`TAG_VOCAB` (``detour``, ``useful``) and :data:`SPAWN_REASONS` so
#: a transcript recorded before the retirement still parses.
RETIRED_METRIC_IDS: tuple[str, ...] = ("detour", "web", "spawn")

#: The persistent feedback note (``feedback_note``): the status line's
#: second line and the dashboard banner show it word for word.
FEEDBACK_NOTE = "Finished a piece of work? Run /tl-feedback: a few ticks make your savings tips fit how you work."


# -- the /tl-feedback questions --------------------------------------------

#: The feedback skill: the user runs it as ``/tl-feedback``, from
#: ``~/.claude/skills/tl-feedback/SKILL.md``.
FEEDBACK_SKILL = "tl-feedback"

#: ``[tl-fb: ...]``: the tag the skill ends with, carrying the answers.
FEEDBACK_TAG = "tl-fb"


@dataclass(frozen=True, slots=True)
class FeedbackQuestion:
    """One /tl-feedback question, asked with AskUserQuestion and offered
    as checkboxes on the dashboard's Sessions tab."""

    #: The ``[tl-fb: ...]`` key its answer is written under.
    key: str
    #: AskUserQuestion's chip label: at most 12 characters, starting "TL"
    #: so the answers can be told apart from any other question.
    header: str
    question: str
    #: Several answers may be ticked.
    multi: bool
    #: ``(word, label, description)`` per option: the word goes in the
    #: tag, the label is what the user ticks. Labels hold no commas,
    #: because several ticked answers can come back as one comma-joined
    #: string.
    options: tuple[tuple[str, str, str], ...]


FEEDBACK_QUESTIONS: tuple[FeedbackQuestion, ...] = (
    FeedbackQuestion(
        key="outcome",
        header="TL outcome",
        question="Did this piece of work deliver what you expected?",
        multi=False,
        options=(
            ("met", "Yes", "It did what I asked"),
            ("partly", "Partly", "Some of it, or with gaps I had to fill"),
            ("missed", "No", "It missed what I wanted"),
            ("stopped", "Stopped early", "I stopped it or changed course"),
        ),
    ),
    FeedbackQuestion(
        key="slow",
        header="TL slowdown",
        question="What slowed it down?",
        multi=True,
        options=(
            ("unclear", "My request was unclear", "Claude had to ask, guess or search"),
            ("rework", "Wrong approach or rework", "A dead end, or work that had to be redone"),
            ("tools", "Tool or setup trouble", "Failing commands, tests or permissions"),
            ("none", "Nothing", "It went smoothly"),
        ),
    ),
    FeedbackQuestion(
        key="worth",
        header="TL worth",
        question="Was the result worth the tokens it used?",
        multi=False,
        options=(
            ("yes", "Worth it", "Good value for what it cost"),
            ("fair", "About right", "Roughly what I'd expect"),
            ("no", "Too costly", "Too many tokens for the result"),
        ),
    ),
    FeedbackQuestion(
        key="helped",
        header="TL helped",
        question="What would have helped?",
        multi=True,
        options=(
            ("context", "More context up front", "Files, errors or examples in the first message"),
            ("plan", "A plan first", "Agreeing the approach before any edits"),
            ("smaller", "Smaller steps", "One part at a time, or a fresh session per part"),
            ("none", "Nothing", "It was fine as it was"),
        ),
    ),
)

#: Asked in a second AskUserQuestion call, and only when a plan was
#: approved during the piece of work (the first call already holds four
#: questions, AskUserQuestion's limit). Not a dashboard rating: the
#: Sessions tab can't tell whether a plan was approved.
HANDOFF_QUESTION = FeedbackQuestion(
    key="handoff",
    header="TL handoff",
    question="Could the build have started in a fresh session from just the plan?",
    multi=False,
    options=(
        ("yes", "Yes", "The plan had everything needed"),
        ("partly", "Partly", "It needed a few things from earlier"),
        ("no", "No", "It relied on the earlier discussion"),
    ),
)

#: Every question a ``[tl-fb: ...]`` tag or an answer may carry.
ALL_FEEDBACK_QUESTIONS: tuple[FeedbackQuestion, ...] = FEEDBACK_QUESTIONS + (HANDOFF_QUESTION,)

#: ``[tl-fb: ...]`` key -> the words its answer may take.
FEEDBACK_VOCAB: dict[str, tuple[str, ...]] = {q.key: tuple(o[0] for o in q.options) for q in ALL_FEEDBACK_QUESTIONS}

#: ``[tl-fb: ...]`` keys whose value is a comma list of words.
FEEDBACK_LIST_KEYS = frozenset(q.key for q in ALL_FEEDBACK_QUESTIONS if q.multi)

#: The words a dashboard rating (the Sessions tab's checkboxes) may
#: take: the four questions every run asks.
RATING_VOCAB: dict[str, tuple[str, ...]] = {q.key: FEEDBACK_VOCAB[q.key] for q in FEEDBACK_QUESTIONS}


def feedback_skill_text() -> str:
    """``SKILL.md`` for ``/tl-feedback``. ``disable-model-invocation``
    keeps its description out of Claude's context until the user runs
    it, and it names no model: switching model mid-session would rebuild
    the whole prompt cache, which costs more than the skill saves."""
    lines = [
        "---",
        f"name: {FEEDBACK_SKILL}",
        "description: Rate the piece of work you just finished for ClaudeGlass, with four quick "
        "checkbox questions (five after an approved plan).",
        "disable-model-invocation: true",
        "allowed-tools: AskUserQuestion",
        "---",
        "",
        "The user wants to rate the piece of work just finished, for ClaudeGlass, which turns the "
        "answers into token-saving tips. Do only what follows: no summary of the work, no other tools.",
        "",
        "1. Call AskUserQuestion once, with these four questions word for word, in this order:",
        "",
    ]
    for q in FEEDBACK_QUESTIONS:
        choice = "several answers allowed (multiSelect true)" if q.multi else "one answer (multiSelect false)"
        lines.append(f'   - header "{q.header}", question "{q.question}", {choice}. Options:')
        for _word, label, description in q.options:
            lines.append(f'     - "{label}": {description}')
    q = HANDOFF_QUESTION
    lines += [
        "",
        "2. Only if you approved a plan with ExitPlanMode during this piece of work, call AskUserQuestion "
        "a second time with this one question. Otherwise skip this step and leave the handoff key out:",
        "",
        f'   - header "{q.header}", question "{q.question}", one answer (multiSelect false). Options:',
    ]
    for _word, label, description in q.options:
        lines.append(f'     - "{label}": {description}')
    lines += [
        "",
        "3. End your reply with this one line, putting in the word for each answer ticked, joined with "
        "commas where several were ticked. Leave out a key whose question was skipped, not asked or "
        "answered only with free text:",
        "",
        f"   [{FEEDBACK_TAG}: "
        + " ".join(f"{q.key}=<{'words' if q.multi else 'word'}>" for q in ALL_FEEDBACK_QUESTIONS)
        + "]",
        "",
        "   The word for each answer:",
        "",
    ]
    for q in ALL_FEEDBACK_QUESTIONS:
        words = ", ".join(f'"{label}" = {word}' for word, label, _description in q.options)
        lines.append(f"   - {q.key}: {words}")
    lines += [
        "",
        '4. After the tag, write one line: "Thanks: ClaudeGlass will use this for your savings tips."',
        "",
        'If the user declines the questions, reply only "No problem." and write no tag.',
        "",
    ]
    return "\n".join(lines)


# -- the /tl-brief checklists -----------------------------------------------

#: The brief skill: the user runs it as ``/tl-brief <request>``, from
#: ``~/.claude/skills/tl-brief/SKILL.md``.
BRIEF_SKILL = "tl-brief"

#: A checklist line -> ``(label, template line)``. The keys are the
#: ``missing`` words (``none`` aside), plus ``report`` for research.
BRIEF_LINES: dict[str, tuple[str, str]] = {
    "files": ("Files", "Files: <the paths you know are involved>"),
    "goal": ("Goal", "Goal: <what should be true afterwards, and why>"),
    "constraints": ("Constraints", "Keep: <what must not change; libraries and patterns to stick to>"),
    "done": ("Done when", "Done when: <the test, command or behaviour that shows it works>"),
    "repro": ("Reproduce", "Reproduce: <steps or command>. Error: <paste the failing output>"),
    "scope": ("Scope", "Only: <what's in scope>. Not: <what to leave alone>"),
    "report": ("Report", "Report: <how long, and in what form>"),
}

#: The checklist each kind of task starts from. The Work habits page puts
#: the lines your own requests most often lack first; the skill uses these
#: as they are, so what it holds doesn't change with your data.
BRIEF_CHECKLISTS: dict[str, tuple[str, ...]] = {
    "bugfix": ("repro", "files", "done"),
    "debug": ("repro", "files", "done"),
    "feature": ("goal", "files", "constraints", "done"),
    "refactor": ("files", "constraints", "done"),
    "research": ("goal", "files", "report"),
    "review": ("files", "scope"),
    "test": ("files", "done"),
    "docs": ("goal", "files"),
    "plan": ("goal", "constraints"),
    "ops": ("goal", "constraints", "done"),
    "chat": ("goal",),
}


def brief_skill_text() -> str:
    """``SKILL.md`` for ``/tl-brief``: check a request against its kind of
    task's checklist and ask once for what is missing, or start. Like
    ``/tl-feedback`` it is user-invoked only and names no model."""
    lines = [
        "---",
        f"name: {BRIEF_SKILL}",
        "description: Check a request against a short checklist for its kind of task before starting, for "
        "ClaudeGlass.",
        "disable-model-invocation: true",
        "---",
        "",
        "The user wants their request checked before the work starts, for ClaudeGlass, so less is spent "
        "finding things out. The request is the text after /tl-brief; when there is none, it is the user's "
        "previous message.",
        "",
        "1. Decide which kind of task it is: " + ", ".join(BRIEF_CHECKLISTS) + ".",
        "",
        "2. Check the request against that kind's checklist:",
        "",
    ]
    for task, keys in BRIEF_CHECKLISTS.items():
        lines.append(f"   - {task}: " + ", ".join(BRIEF_LINES[k][0] for k in keys))
    lines += [
        "",
        "3. If every line is covered, or what is missing can be found with one quick look, start the work "
        "straight away and don't mention this check.",
        "",
        "4. Otherwise ask once, in one short message, for only the missing lines, as lines the user can fill "
        "in, then wait for the answer before starting:",
        "",
    ]
    for _label, template in BRIEF_LINES.values():
        lines.append(f"   {template}")
    lines += [
        "",
        "Ask about nothing else, and don't repeat the request back.",
        "",
    ]
    return "\n".join(lines)

#: The note's fixed lines. ``{tag}`` in :data:`SUB_TAG_INTRO` is the
#: ``[result: ...]`` shape (:data:`SUB_TAG` or :data:`SUB_TAG_WITH_KEYS`).
NOTE_INTRO = "The user turned on ClaudeGlass metrics capture, to see where their tokens go."
MAIN_TAG_INTRO = (
    "End your final reply to each user message with one line, [tl: key=word ...], using only these keys and words:"
)
SUB_TAG_INTRO = "End your final report with one line, {tag}, using only these words:"
SUB_TAG = "[result: done|partial|blocked]"
SUB_TAG_WITH_KEYS = "[result: done|partial|blocked key=word ...]"
SKIP_KEY_LINE = "Leave out a key you can't judge."


def level_metrics(level: str) -> tuple[str, ...]:
    """The metric ids a preset level turns on (``()`` for ``off`` or an
    unknown level)."""
    if level not in LEVELS or level == "off":
        return ()
    upto = LEVEL_GROUPS[: LEVEL_GROUPS.index(level) + 1]
    return tuple(m.id for m in METRICS if m.group in upto)


def level_includes(level: str) -> tuple[str, ...]:
    """Everything picking ``level`` turns on: :func:`level_metrics`,
    plus :data:`DEEP_FEEDBACK_IDS` for Deep. What the level cards, the
    estimates and ``docs/capture.md`` show and price."""
    extra = DEEP_FEEDBACK_IDS if level == "deep" else ()
    return level_metrics(level) + extra


def with_requirements(ids) -> tuple[str, ...]:
    """``ids`` in catalogue order, plus any metric they need (a subagent's
    extras need ``result``); unknown ids and ids outside the levels are
    dropped."""
    wanted = {i for i in ids if i in METRICS_BY_ID and METRICS_BY_ID[i].group in LEVEL_GROUPS}
    for i in list(wanted):
        wanted.update(METRICS_BY_ID[i].requires)
    return tuple(m.id for m in METRICS if m.id in wanted)


def level_of(ids) -> str:
    """The preset level whose metrics are exactly ``ids``, else
    ``custom``; ``off`` for none."""
    chosen = set(with_requirements(ids))
    if not chosen:
        return "off"
    for level in LEVELS[1:]:
        if chosen == set(level_metrics(level)):
            return level
    return CUSTOM_LEVEL


def active_metrics(level: str, metrics=(), feedback=()) -> tuple[str, ...]:
    """Every metric switched on: the level's own (or ``metrics`` when the
    level is ``custom``), then the feedback toggles that ride in the
    note."""
    ids = with_requirements(metrics) if level == CUSTOM_LEVEL else level_metrics(level)
    return ids + tuple(i for i in FEEDBACK_IDS if i in set(feedback))


def note_text(ids, scope: str, agent_type: str = "", tagger: str = DEFAULT_TAGGER) -> str:
    """The note the hook adds for ``scope`` (``"main"`` at session start,
    ``"subagent"`` at agent start) with the metrics in ``ids`` switched
    on; ``""`` when none of them asks anything there. While Haiku writes
    the tags (``tagger``), the main note asks for no ``[tl: ...]`` tag.

    ``hooks/capture-hook.py`` builds the same text from
    ``capture-catalogue.json`` (:func:`export_json`); a test holds the two
    to the same output.
    """
    if scope == "subagent" and agent_type in SKIP_AGENT_TYPES:
        return ""
    wanted = set(ids)
    enabled = [m for m in METRICS if m.id in wanted]
    if scope == "subagent" and agent_type in NO_RULES_AGENT_TYPES:
        enabled = [m for m in enabled if m.id not in ("rules", "agent_brief")]
    main = scope == "main"
    untagged = main and tagger == "haiku"
    lines = ["" if untagged else m.main_line if main else m.sub_line for m in enabled]
    extras = [
        (m.main_extra_untagged or m.main_extra) if untagged else m.main_extra if main else m.sub_extra
        for m in enabled
    ]
    codes = [m.id for m, line, x in zip(enabled, lines, extras) if line or x]
    if not codes:
        return ""
    out = [f"{NOTE_MARKER}{NOTE_VERSION} {','.join(codes)}", NOTE_INTRO]
    # CAP-1: an extra marked extra_before_tag (feedback_reminder) tells
    # Claude to end its reply with something too, so it goes before the
    # tag block, not after -- the tag instruction stays the last thing
    # the note asks for.
    before_tag = [x for m, x in zip(enabled, extras) if x and main and m.extra_before_tag]
    after_tag = [x for m, x in zip(enabled, extras) if x and not (main and m.extra_before_tag)]
    out += before_tag
    if any(lines):
        if main:
            out.append(MAIN_TAG_INTRO)
        else:
            keys = any(line for m, line in zip(enabled, lines) if m.id != "result")
            out.append(SUB_TAG_INTRO.format(tag=SUB_TAG_WITH_KEYS if keys else SUB_TAG))
        out += [line for line in lines if line]
        if main:
            out.append(SKIP_KEY_LINE)
    out += after_tag
    return "\n".join(out)


def tagged_keys(ids) -> tuple[str, ...]:
    """The ``[tl: ...]`` keys the metrics in ``ids`` ask for, in
    catalogue order."""
    wanted = set(ids)
    return tuple(m.id for m in METRICS if m.id in wanted and m.main_line)


def judge_text(ids) -> str:
    """What Haiku is told when it writes the main session's tags
    (``tagger = "haiku"``): the same key lines Claude's note would carry;
    ``""`` when none of ``ids`` asks for a key. ``hooks/capture-hook.py``
    builds the same text (``build_judge_prompt``)."""
    keys = set(tagged_keys(ids))
    if not keys:
        return ""
    return "\n".join([JUDGE_INTRO, *(m.main_line for m in METRICS if m.id in keys), JUDGE_RULE])


def tool_note_text(metric_id: str) -> str:
    """The note a PostToolUse hook adds after a large result
    (``big_output``) or a web result (``web``)."""
    metric = METRICS_BY_ID.get(metric_id)
    if metric is None or not metric.tool_note:
        return ""
    return f"{NOTE_MARKER}{NOTE_VERSION} {metric.id}\n{metric.tool_note}"


#: The literal tool names each PostToolUse-triggered metric matches
#: (see ``hook_specs``'s own matchers): an MCP wildcard entry
#: ("mcp__.*") isn't a real tool name, so it's left out.
_POST_TOOL_USE_TOOLS = {"big_output": BIG_OUTPUT_TOOLS}


def tool_suffix_chars(metric_id: str) -> int:
    """CAP-10: Claude Code's own PostToolUse wrap names the specific
    tool that matched, not the whole matcher pattern (its own debug log
    shows ``"PostToolUse:Write"``, not ``"PostToolUse:Bash|Read|..."``)
    -- estimated here, before any real note has been measured, as the
    average length of ``metric_id``'s own matcher's literal tool names,
    plus the ``:`` that joins it to the event name."""
    names = [t for t in _POST_TOOL_USE_TOOLS.get(metric_id, ()) if "*" not in t]
    return round(sum(len(t) for t in names) / len(names)) + 1 if names else 0


def hook_specs(ids) -> tuple[tuple[str, str, str, bool], ...]:
    """The Claude Code hook entries the metrics in ``ids`` need, as
    ``(script, event, matcher, async)``: the note at session and agent
    start, after tool results for Deep's tool notes, and the free
    signals' events. Every entry that adds a note runs in the
    foreground: an async hook's ``additionalContext``/``systemMessage``
    does reach Claude (docs/en/hooks.md), but only on the next
    conversation turn, which would put a session/agent-start note one
    turn late and a Deep tool note a full reply behind the result it's
    about, so these stay synchronous; the tool note's matcher keeps
    that wait to the tools whose results can be large. Coaching notes
    (``coaching_notes``) add the message you send and approved plans to
    that, in one PostToolUse entry shared with the tool note. SessionEnd
    runs as the session closes, when nothing waits on it; the other
    signals run in the background. While Haiku writes the tags
    (:data:`HAIKU_TAGGER_HOOK` in ``ids``), ``Stop`` runs in the
    foreground, shared with ``turn_signals``: it only hands the turn to a
    worker of its own and returns, and ``claude -p`` exits without waiting
    for a background hook, which would drop the turn."""
    wanted = set(ids)
    haiku = HAIKU_TAGGER_HOOK in wanted and bool(tagged_keys(wanted))
    main = any(m.id in wanted and ((m.main_line and not haiku) or m.main_extra) for m in METRICS)
    sub = any(m.id in wanted and (m.sub_line or m.sub_extra) for m in METRICS)
    coach = "coaching_notes" in wanted
    specs: list[tuple[str, str, str, bool]] = []
    if main:
        specs.append((HOOK_SCRIPT, "SessionStart", SESSION_START_MATCHER, False))
    if sub:
        specs.append((HOOK_SCRIPT, "SubagentStart", "", False))
    if coach:
        specs.append((HOOK_SCRIPT, "UserPromptSubmit", "", False))
    tools = (*(BIG_OUTPUT_TOOLS if "big_output" in wanted else ()), *(COACHING_TOOLS if coach else ()))
    if tools:
        specs.append((HOOK_SCRIPT, "PostToolUse", "|".join(dict.fromkeys(tools)), False))
    for event in SIGNAL_EVENTS:
        if SIGNAL_EVENTS[event] in wanted:
            specs.append((HOOK_SCRIPT, event, "", event != "SessionEnd" and not (haiku and event == "Stop")))
    if haiku and not any(spec[1] == "Stop" for spec in specs):
        specs.append((HOOK_SCRIPT, "Stop", "", False))
    return tuple(specs)


def export_json() -> dict:
    """What ``hooks/capture-hook.py`` needs from this module, as JSON-safe
    data. The packaged ``hooks/capture-catalogue.json`` is this, written
    by :func:`catalogue_json_text` (a test keeps it in step)."""
    return {
        "version": NOTE_VERSION,
        "marker": NOTE_MARKER,
        "levels": {level: list(level_metrics(level)) for level in LEVELS[1:]},
        "feedback_ids": list(FEEDBACK_IDS),
        "metrics": [
            {
                "id": m.id,
                "group": m.group,
                "requires": list(m.requires),
                "main_line": m.main_line,
                "main_extra": m.main_extra,
                "extra_before_tag": m.extra_before_tag,
                "main_extra_untagged": m.main_extra_untagged,
                "sub_line": m.sub_line,
                "sub_extra": m.sub_extra,
                "tool_note": m.tool_note,
            }
            for m in METRICS
            if m.group in LEVEL_GROUPS or m.main_extra or m.sub_extra
        ],
        "text": {
            "intro": NOTE_INTRO,
            "main_tag_intro": MAIN_TAG_INTRO,
            "sub_tag_intro": SUB_TAG_INTRO,
            "sub_tag": SUB_TAG,
            "sub_tag_with_keys": SUB_TAG_WITH_KEYS,
            "skip_key_line": SKIP_KEY_LINE,
        },
        "skip_agent_types": list(SKIP_AGENT_TYPES),
        "no_rules_agent_types": list(NO_RULES_AGENT_TYPES),
        "big_output_tokens": BIG_OUTPUT_TOKENS,
        "signal_events": dict(SIGNAL_EVENTS),
        "session_end_reasons": list(SESSION_END_REASONS),
        "wait_kinds": list(WAIT_KINDS),
        "turn_states": list(TURN_STATES),
        "stop_failure_errors": list(STOP_FAILURE_ERRORS),
        "signals_dir": SIGNALS_DIR,
        "judge": {
            "model": JUDGE_MODEL,
            "dir": JUDGE_DIR,
            "env": JUDGE_ENV,
            "timeout_s": JUDGE_TIMEOUT_S,
            "thinking_tokens": JUDGE_THINKING_TOKENS,
            "limits": dict(JUDGE_LIMITS),
            "intro": JUDGE_INTRO,
            "rule": JUDGE_RULE,
            "vocab": {key: list(words) for key, words in TAG_VOCAB.items()},
            "list_keys": sorted(LIST_KEYS),
        },
        "coaching": {
            "marker": COACH_MARKER,
            "version": COACH_VERSION,
            "file": COACHING_FILE,
            "state_file": COACH_STATE_FILE,
            "read_tools": list(COACHING_READ_TOOLS),
            "thresholds": dict(COACHING_THRESHOLDS),
            "text": dict(COACHING_TEXT),
            "quiet_how": dict(COACHING_QUIET_HOW),
            "notice": dict(COACHING_NOTICE),
            "correction_pattern": CORRECTION_PATTERN,
            "correction_scan_chars": CORRECTION_SCAN_CHARS,
            "fix_pattern": FIX_PATTERN,
            "specific_pattern": SPECIFIC_PATTERN,
            "interrupt_prefix": INTERRUPT_PREFIX,
            "edit_tools": list(EDIT_TOOLS),
            "ack_pattern": ACK_PATTERN,
            "list_item_pattern": LIST_ITEM_PATTERN,
            "action_pattern": ACTION_PATTERN,
            "item_separator_pattern": ITEM_SEPARATOR_PATTERN,
            "sentence_end_pattern": SENTENCE_END_PATTERN,
        },
    }


#: Characters Claude Code wraps a hook note in: the system-reminder tags
#: and "<event> hook additional context: ", less the event name itself.
NOTE_WRAP_CHARS = 63

#: Characters the "[tl: " and "]" around a reply tag add.
_TAG_FRAME_CHARS = 6


def asks_claude(metric_id: str) -> bool:
    """Whether a metric has Claude read or write something, and so uses
    tokens."""
    m = METRICS_BY_ID.get(metric_id)
    return bool(m and (m.main_line or m.sub_line or m.main_extra or m.sub_extra or m.tool_note))


def rough_tokens(ids, tagger: str = DEFAULT_TAGGER) -> dict[str, int]:
    """Rough sizes in tokens (characters / 4) for the metrics in ``ids``:
    the note at each session start, clear or compaction
    (``session_note``) and at each subagent start (``subagent_note``);
    the tag Claude writes per reply (``reply_tag``) and per subagent
    report (``report_tag``); the note after a large or web tool result
    (``tool_note``). Amounts measured from transcripts replace these once
    capture has run. While Claude Haiku writes the tags (``tagger``), the
    reply carries none: ``reply_tag`` is only the reminder line, if on."""
    enabled = [METRICS_BY_ID[i] for i in ids if i in METRICS_BY_ID]
    main, sub = note_text(ids, "main", tagger=tagger), note_text(ids, "subagent")
    if tagger == "haiku" and any(m.main_line for m in enabled):
        # No tag: only the reminder line ends a reply.
        reply = sum(m.out_chars for m in enabled if m.main_extra and m.group == "feedback")
        frame = 0
    else:
        reply = sum(m.out_chars for m in enabled if m.main_line or m.main_extra)
        frame = _TAG_FRAME_CHARS
    report = sum(m.out_chars for m in enabled if m.sub_line or m.sub_extra)
    tool = max(
        (len(tool_note_text(m.id)) + tool_suffix_chars(m.id) for m in enabled if m.tool_note),
        default=0,
    )
    return {
        "session_note": round((len(main) + NOTE_WRAP_CHARS + len("SessionStart")) / 4) if main else 0,
        "subagent_note": round((len(sub) + NOTE_WRAP_CHARS + len("SubagentStart")) / 4) if sub else 0,
        "reply_tag": round((reply + frame) / 4) if reply else 0,
        "report_tag": round(report / 4),
        "tool_note": round((tool + NOTE_WRAP_CHARS + len("PostToolUse")) / 4) if tool else 0,
    }


def catalogue_json_text() -> str:
    """:func:`export_json` as the exact text of the packaged file."""
    return json.dumps(export_json(), indent=1, ensure_ascii=False) + "\n"


# -- docs/capture.md ---------------------------------------------------------

#: :data:`Metric.group` -> how :func:`render_markdown` names it in a
#: metric's "Level" line. The four preset levels already have a display
#: name in :data:`LEVEL_TITLES`; only the three groups that aren't a
#: level need one here.
_GROUP_LABELS = {
    "derived": "Always measured, no hook",
    "feedback": "Feedback, any level",
    "coaching": "Live coaching, any level",
}


def _metric_group_label(metric: Metric) -> str:
    if metric.id in DEEP_FEEDBACK_IDS:
        return f"{_GROUP_LABELS[metric.group]}; switching to Deep turns it on"
    return LEVEL_TITLES.get(metric.group) or _GROUP_LABELS[metric.group]


def _feedback_tag_words() -> str:
    """The full ``[tl-fb: ...]`` tag with every question's whole
    vocabulary spelled out. ``Metric.tag`` shortens this with an
    ellipsis for the Capture page's table; the doc's "exact words"
    promise needs the real thing, so :func:`render_markdown` builds it
    here instead of using ``METRICS_BY_ID["feedback_skill"].tag``."""
    parts = [
        f"{q.key}=" + (",".join(o[0] for o in q.options) if q.multi else "|".join(o[0] for o in q.options))
        for q in ALL_FEEDBACK_QUESTIONS
    ]
    return f"[{FEEDBACK_TAG}: " + " ".join(parts) + "]"


def _metric_tag_line(metric: Metric) -> str:
    """What :func:`render_markdown` prints for a metric's "Tag" line:
    the exact key and words Claude writes, or why there is none."""
    if metric.id == "feedback_skill":
        return f"`{_feedback_tag_words()}`"
    if metric.tag:
        return f"`{metric.tag}`"
    if metric.main_extra:
        return f'No fixed key. The note asks for a line: "{metric.main_extra}"'
    if metric.sub_extra:
        return f'No fixed key. The note asks for a line: "{metric.sub_extra}"'
    if metric.id == "coaching_notes":
        return (
            "No tag. A hook adds a note only when a hint applies, and Claude acts on it or tells you in a "
            "highlighted tip. "
            "Each hint and when it applies: [coaching.md](coaching.md)."
        )
    if metric.hooks:
        return "No tag. A hook records it directly; Claude is never asked."
    if metric.group == "coaching":
        return "No tag. Shown only in the status line; Claude is never asked, and it costs no tokens."
    if metric.group == "feedback":
        return 'No tag. Nothing is asked of Claude; see "Captures" above for how it is kept.'
    return "No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens."


def render_markdown() -> str:
    """The full metric catalogue as Markdown (``docs/capture.md``), built
    only from this module's own data: what metrics capture is and that
    it is opt-in, what each level adds and roughly costs
    (:func:`note_text` lengths, characters / 4), every metric grouped by
    :data:`SECTIONS`, the tag format, the privacy stance, and the
    ``claudeglass capture ...`` commands that turn it on, off or
    remove it. A test holds the checked-in ``docs/capture.md`` to this
    function's output, the same way ``hooks/capture-catalogue.json`` is
    held to :func:`catalogue_json_text`.
    """
    out: list[str] = []
    p = out.append

    p("# Metrics capture")
    p("")
    p(
        "Metrics capture is **opt-in**. Off by default, and off costs nothing: no hook runs, no tag is asked "
        "for, no token is spent on it."
    )
    p("")
    p(
        f"Turned on, a hook (`{HOOK_SCRIPT}`) adds a short note to each session and subagent start, and asks "
        "Claude to end its replies with one line such as `[tl: task=bugfix brief=partial level=normal]`. A "
        "subagent ends its own report the same way, starting `[result: done|partial|blocked]`. The tag always "
        "sits at the end of the reply you already read — nothing is hidden — and nothing free-text is ever "
        "asked for: every word comes from a closed vocabulary (see [Privacy](#privacy) below)."
    )
    p("")
    p(
        "It costs tokens. The note is written to the prompt cache once, then read from it on every later "
        "reply of that session; the tag itself is a handful of output tokens on every reply and every "
        "subagent report. [Levels](#levels) below gives rough sizes; once capture is on, Setup › Capture "
        "measures the real cost from your own transcripts, and a banner on every page shows the "
        "running total."
    )
    p("")

    # -- Levels -----------------------------------------------------------
    p("## Levels")
    p("")
    p(
        "Costs rise with depth, so capture comes in levels, each including every metric of the levels "
        "before it. The note is added once at each session's start, `/clear` or compaction (a resumed "
        "session already carries the note from its start, so it is not asked again), and once at each "
        "subagent's start, however deep the agent is nested."
    )
    p("")
    p("| Level | What it adds | Note at session start | Note per subagent start |")
    p("|---|---|---|---|")
    for level in LEVELS:
        ids = level_includes(level)
        # D17: the same rough_tokens() the Capture page and CAP-7's
        # step-down suggestion use, not a separate chars/4 calculation
        # that quietly drops the hook-wrapper overhead rough_tokens()
        # includes -- the two drifted apart (an earlier audit measured
        # 201/107 for Essentials against a checked-in 182/88 here).
        sizes = rough_tokens(ids)
        main_cell = f"~{sizes['session_note']} tokens" if sizes["session_note"] else "–"
        sub_cell = f"~{sizes['subagent_note']} tokens" if sizes["subagent_note"] else "–"
        p(f"| {LEVEL_TITLES[level]} | {LEVEL_SUMMARIES[level]} | {main_cell} | {sub_cell} |")
    p(
        f"| {LEVEL_TITLES[CUSTOM_LEVEL]} | Any other set of metrics, turned on one by one (`capture enable`/"
        "`capture disable`). | depends what's on | depends what's on |"
    )
    p("")
    p(
        "These are rough sizes — the note's characters divided by four, plus Claude Code's own hook-wrapper "
        "overhead (the system-reminder tags around it) — and don't include the tag Claude writes back (each "
        "metric below says roughly how many output tokens its own words cost). Setup › Capture replays your "
        "last 14 days of transcripts against each level before you turn it on, and once it's on, measures the "
        "real note and tag cost from what Claude Code actually recorded — read that number, not this one, "
        "when it matters."
    )
    p("")

    # -- Worth (CAP-5, gap 4) ------------------------------------------------
    p("## What each metric is worth")
    p("")
    p(
        "Every metric here has to earn its keep. Something has to read it and turn it into a decision, not "
        "only log it. This table is that trace: each metric's rough cost against what it "
        "feeds. The Capture page shows the same thing measured from your own transcripts, in tokens a week "
        "instead of per occurrence."
    )
    p("")
    p("| Metric | Level | ~Output tokens each time | Feeds |")
    p("|---|---|---|---|")
    for m in METRICS:
        cost_cell = f"~{max(1, round(m.out_chars / 4))}" if m.out_chars else "–"
        feeds = ", ".join(THEMES.get(theme, theme) for theme in m.powers) if m.powers else "–"
        p(f"| {m.title} (`{m.id}`) | {_metric_group_label(m)} | {cost_cell} | {feeds} |")
    p("")

    # -- Metrics grouped by scope ------------------------------------------
    for section_key, section_title in SECTIONS.items():
        metrics = [m for m in METRICS if m.section == section_key]
        if not metrics:
            continue
        p(f"## {section_title}")
        p("")
        for m in metrics:
            p(f"### {m.title} (`{m.id}`)")
            p("")
            p(f"- **Level:** {_metric_group_label(m)}")
            p(f"- **Captures:** {m.what}")
            p(f"- **Why:** {m.why}")
            p(f"- **Tag:** {_metric_tag_line(m)}")
            if m.out_chars:
                out_tokens = max(1, round(m.out_chars / 4))
                unit = "token" if out_tokens == 1 else "tokens"
                p(f"- **Costs:** about {out_tokens} output {unit} each time")
            if m.hooks:
                p(f"- **Hook:** {', '.join(m.hooks)}")
            powers = ", ".join(THEMES[t] for t in m.powers) if m.powers else "—"
            p(f"- **Powers:** {powers}")
            if m.requires:
                needed = ", ".join(f"`{r}`" for r in m.requires)
                p(f"- **Needs:** {needed} switched on too")
            p("")

    # -- Tag format ---------------------------------------------------------
    p("## The tag format")
    p("")
    p(
        f"Every note (`{HOOK_SCRIPT}` builds the same text from `{CATALOGUE_FILE}`) opens with the same two "
        "lines, then the keys for whichever metrics are on:"
    )
    p("")
    p(f"> {NOTE_INTRO}")
    p(">")
    p(f"> {MAIN_TAG_INTRO}")
    p("")
    p(f'...and, in the main session, closes with: "{SKIP_KEY_LINE}"')
    p("")
    p(
        f"A subagent's note asks for `{SUB_TAG}` when nothing else needs a key of its own, or "
        f"`{SUB_TAG_WITH_KEYS}` once Standard's extra keys are on: \"{SUB_TAG_INTRO.format(tag=SUB_TAG_WITH_KEYS)}\""
    )
    p("")
    p(
        "Starting an agent again after its last run fell short is marked at the start of its brief instead "
        f"of the end of a report: `{METRICS_BY_ID['retry'].tag}`."
    )
    p("")
    p(f"The `/tl-feedback` skill ends with its own line: `{_feedback_tag_words()}`.")
    p("")
    p("If Claude writes more than one tag, the last one wins, key by key.")
    p("")

    # -- Who writes the tags ---------------------------------------------------
    p("## Who writes the tags")
    p("")
    standard = level_includes("standard")
    p(
        "By default Claude writes the `[tl: ...]` tag itself, at the end of its final reply to each of your "
        "messages. `claudeglass capture tagger haiku` (or \"Tags written by\" on Setup › Capture) hands that to "
        "Claude Haiku instead, and `capture tagger claude` hands it back:"
    )
    p("")
    p(
        "- The session note no longer carries the tag list, and replies end as they would anyway. At Standard "
        f"the note drops from ~{rough_tokens(standard)['session_note']} to "
        f"~{rough_tokens(standard, 'haiku')['session_note']} tokens."
    )
    p(
        "- When a turn of the main session ends, the hook's `Stop` entry reads the end of the transcript, hands "
        "a short excerpt to a worker process of its own, and returns at once. The excerpt holds your message "
        f"(up to {JUDGE_LIMITS['prompt']:,} characters) and the one before it, how many you sent before, what "
        "Claude did (model calls, output tokens, tools used, files changed, the first line of up to "
        f"{JUDGE_LIMITS['commands']} shell commands, skills, subagents, tool errors, any plan) and the end of its "
        f"final reply (up to {JUDGE_LIMITS['reply']:,} characters). Tool output is never in it."
    )
    p(
        f"- The worker runs `claude -p --model {JUDGE_MODEL}` with no tools, settings, MCP servers or saved "
        "session, through your own Claude Code login, with the excerpt on stdin. Haiku gets the key lines "
        f"Claude's note would have carried, after \"{JUDGE_INTRO}\" and before \"{JUDGE_RULE}\" It loads no "
        "settings file, so your hooks don't run inside it; a login that needs an `apiKeyHelper` from "
        "settings.json fails there, and `capture status` says so."
    )
    p(
        f"- Only the tag's words are kept, checked against the same vocabularies, in `<config-dir>/{JUDGE_DIR}/"
        "YYYY-MM.jsonl`, with the reply's id and what the call cost. What the transcript settles overrides "
        "Haiku: no plan is `plan=none`, one written that turn is `plan=made`, and no skill run is never "
        "`skill=helped`. A turn that got no tag says why: "
        + ", ".join(f"`{e}`" for e in JUDGE_ERRORS)
        + ". `capture status` counts both."
    )
    p(
        f"- Each call costs about ${JUDGE_USD_PER_CALL:.4f} (about 1,200 tokens read, 30 written), counted as "
        "capture's cost. On a subscription it counts toward your usage like any other Haiku use."
    )
    p(
        "- The `Stop` entry runs in the foreground, since `claude -p` exits without waiting for a background "
        "hook, but only for as long as it takes to read the transcript's end. Subagent reports and brief markers "
        "are still Claude's to write. Deep's note after a large result goes only to subagents, as the main "
        "session's replies carry no tag for its word."
    )
    p("")

    # -- Privacy ------------------------------------------------------------
    p("## Privacy")
    p("")
    p(
        "Claude writes closed vocabularies only. Every `[tl: ...]`, `[result: ...]`, `[retry: ...]`, "
        "`[spawn: ...]` and `[tl-fb: ...]` word is checked against the lists on this page; anything else — "
        "an unknown word, a key outside those lists, free text, a path — is dropped by the parser and never "
        "stored. The one exception that can carry a name is `skill=would-help:<name>`, and only when "
        "`<name>` matches a skill this transcript actually listed or invoked in the window; any other name "
        "is cut down to a bare `would-help`."
    )
    p("")
    p(
        "Free local signals never involve Claude at all: a hook logs the session id (hashed with this "
        "tool's own salt), the event word, and — for a permission prompt — the tool name, never its "
        "arguments, to a local file under `<config-dir>/signals/`. Those files, and the `capture-log.jsonl` "
        "record of every on/off/level change, aren't kept forever: `serve`'s watcher (or `capture prune` by "
        "hand) deletes entries past your configured retention, a default applying when none is set."
    )
    p("")
    p(
        "\"Always measured\" metrics read only what Claude Code's own transcript already contains — "
        "instruction files loaded, commands and skills run, task counts, API errors, and simple yes/no "
        "facts about a message's shape (does it name a file path, does it contain a code block) — and keep "
        "only those flags and counts, never the text itself."
    )
    p("")

    # -- Turning it on, off or removing it -----------------------------------
    p("## Turning it on, off or removing it")
    p("")
    p(
        f"The hook script and its catalogue (`{HOOK_SCRIPT}`, `{CATALOGUE_FILE}`) live side by side under "
        "`<config-dir>/hooks/`. Only `capture on`, `capture tagger` and `capture connect` ever change "
        "`~/.claude/settings.json` — and only after showing the diff and asking first, unless you pass "
        "`--yes`. Every other change writes only this tool's own `config.toml`."
    )
    p("")
    p(
        "- `claudeglass capture status` — the level, what's on, since when, and the cost measured so "
        "far. While big_output or web is on, it also prints Deep's actual measured wait (median and p90, "
        "over the last 7 days). It also flags any hook — ClaudeGlass's own or one of yours — that failed on "
        "most of its calls over the last 14 days, naming it (event name only, never a matcher or tool name), "
        "where to find it in `settings.json`, the trade-off, and the undo; this is only ever a printed "
        "prompt, never an automatic change."
    )
    p(
        "- `claudeglass capture on [--level LEVEL] [--for DURATION | --until DATE | --no-limit] "
        "[--sample N] [--yes] [--dry-run]` — turn it on (default level: Essentials)."
    )
    p("- `claudeglass capture level LEVEL` — change the level.")
    p(
        "- `claudeglass capture tagger claude|haiku` — who writes the main session's tags "
        "(see [Who writes the tags](#who-writes-the-tags))."
    )
    p("")
    p(
        f"A fresh switch from off to on — at `init`, `capture on`/`level`, or the Capture page — gets a "
        f"{DEFAULT_CAPTURE_TIMEBOX_DAYS}-day time-box by default, so turning it on doesn't mean it runs "
        "unattended forever: it switches itself back off on its own unless you say otherwise. `--for "
        "DURATION` (a number and `h`, `d` or `w`, e.g. `30d`) or `--until DATE` picks another length or "
        "end date; `--no-limit` turns the time-box off entirely, so capture runs until you switch it off "
        "yourself. `init` has the same three choices as `--capture-for DURATION`, `--capture-level LEVEL "
        "--capture-no-limit`, or (interactively, or under `--non-interactive` with neither given) the "
        "default. Changing the level of capture that's already on leaves an existing time-box (or the "
        "lack of one) exactly as it is — the default only ever applies to a fresh switch-on."
    )
    p(
        "- `claudeglass capture enable METRIC...` / `capture disable METRIC...` — turn individual "
        "metrics on or off; the level becomes Custom once the set no longer matches a preset."
    )
    p("- `claudeglass capture off` — stop the notes and tags at once, without touching settings.json.")
    p(
        "- `claudeglass capture connect` — add the settings.json hook entries the metrics you've "
        "chosen need."
    )
    p("- `claudeglass capture remove` — switch off and take those hook entries back out.")
    p("- `claudeglass capture feedback on|off` — the `/tl-feedback` skill and its status-line reminder.")
    p("- `claudeglass capture brief on|off` — the `/tl-brief` skill.")
    p(
        "- `claudeglass capture prune [--dry-run]` — delete signal files, Claude Haiku's tag files and "
        "`capture-log.jsonl` records past your configured retention (`retention_days` in `config.toml`, or a default when it's "
        "unset); `serve`'s watcher already runs this same cleanup on every tick, so this is for anyone not "
        "running it."
    )
    p(
        "- `claudeglass changes` and `claudeglass uninstall` also cover metrics capture: they "
        "list everything it installed and can remove all of it — hooks, skills and signal files included."
    )
    p("")

    text = "\n".join(out)
    return text.rstrip("\n") + "\n"

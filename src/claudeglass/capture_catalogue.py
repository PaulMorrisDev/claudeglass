"""Metrics capture: the words Claude may write, and the metrics behind them.

Metrics capture is opt-in. While it is on, a small hook adds a short note
to each session and subagent start (see ``hooks/capture_hook.py``) asking
Claude to end its replies with a one-line tag, for example
``[cg: task=bugfix brief=partial level=normal]``. ClaudeGlass reads the tags
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
#: (``cg-cap v1 task,brief,level``). The parser finds capture notes by it.
NOTE_MARKER = "cg-cap v"
#: The marker notes carried until 0.12.1 (``tl`` for claude-token-lens,
#: this tool's name until 0.9.0), as did every tag and marker below. The
#: parser reads both, so sessions from before read the same.
OLD_NOTE_MARKER = "tl-cap v"

#: Current note format version.
NOTE_VERSION = 1

#: ``[cg: ...]`` keys (and the ``[result: ...]`` extras) -> the words each
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
#: for word. The note asks for it *before* the ``[cg: ...]`` tag (CAP-1),
#: and ``capture_tags`` strips it from a reply's tail before matching the
#: trailing tag, so it doesn't matter if Claude writes them the other
#: way round.
FEEDBACK_REMINDER_LINE = "Finished? Run /cg-feedback: a few ticks make your savings tips fit how you work."

#: How Claude sets a coaching tip or the reminder apart in its reply, so
#: it stands out from the work in the terminal and the desktop app alike:
#: after a blank line, a quote block opening with a bold label. No emoji:
#: Claude copies what its context shows, and with a ⚠️ and a 💡 in these
#: labels it began using them as markers of its own, in work that had
#: nothing to do with ClaudeGlass. The notices shown only to you
#: (:data:`COACHING_NOTICE`) never reach Claude, so they keep theirs. A tip is
#: also the last line of its note, written ready for Claude to copy.
TIP_LABEL = "> **ClaudeGlass tip:**"
REMINDER_LABEL = "> **ClaudeGlass:**"
#: What a notice shown only to you opens with. Its ⚠️ is safe because the
#: notice never reaches Claude.
NOTICE_LABEL = "⚠️ ClaudeGlass: "


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
    "off": "No metrics are captured and no tokens are used for them. Live coaching and feedback have their "
    "own switches and keep working while capture is off.",
    "free": "Local signals from hooks that log to a file. Uses no Claude tokens.",
    "essentials": "Claude tags each piece of work: what kind it was, how clear the request was, how hard, "
    "how big, and when the task changed. Claude Haiku judges whether each agent run finished, and why one was "
    "run again.",
    "standard": "Adds what the request lacked, planning, skills, research, "
    "and Haiku's view of each agent run's model and brief.",
    "deep": "Adds how much earlier context was needed, how the change was checked, and a "
    "short rating after large tool outputs. Also turns on the /cg-feedback survey, its reminder note, "
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
#: from waiting on it. The shell and MCP tools are left out: a replay of
#: 30 days of sessions found the shell's note failed both the precision
#: test and the tokens-against-time test, and the MCP note right a third
#: of the time (half was needed). Together they were about two thirds of
#: the spawns this entry caused.
BIG_OUTPUT_TOOLS = ("Read", "Grep", "Glob", *WEB_TOOLS)

#: The most tokens an image counts for in a tool result, and the side in
#: pixels of the square Claude counts one token per (``ceil(width / 28) *
#: ceil(height / 28)``, capped). An image's base64 length is not text
#: Claude read, so it is never counted by its characters.
RESULT_IMAGE_MAX_TOKENS = 1_600
RESULT_IMAGE_PATCH_PX = 28

#: Characters of a result past which Claude Code saves the output to a
#: file and keeps only a short preview in context, by tool: seen in real
#: transcripts (the largest results kept whole were 18.8k for a search,
#: 47.9k for a fetch and 29.7k for a shell; the smallest saved ones 22.3k,
#: 59.3k and 29.3k). A saved result counts for its preview only, as the
#: preview is all Claude reads of it. A read is never saved.
RESULT_PERSIST_CHARS = {"Grep": 20_000, "WebFetch": 50_000, "Bash": 30_000, "PowerShell": 30_000}

#: Characters of the preview Claude Code keeps in context for a result it
#: saved to a file (the ``<persisted-output>`` note: about 2 KB).
RESULT_PREVIEW_CHARS = 2_200

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

#: Who writes the main session's ``[cg: ...]`` tag (``[capture] tagger``):
#: Claude, at the end of its final reply to each message (the default), or
#: Claude Haiku, which the hook asks after each turn, in the background,
#: with a short excerpt of it (:func:`judge_text`). With ``haiku`` the
#: session note asks for no tag, so replies end as they would anyway and
#: the session carries no tag list; the words land in :data:`JUDGE_DIR`
#: instead. Agent runs are Haiku's to judge either way
#: (:func:`agent_judge_text`): a subagent is never asked for a tag, and a
#: brief never carries a marker.
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
#: has run: about 1,700 tokens read (Claude Code's own frame, the
#: instructions and the excerpt) and 35 written, at Haiku's list price,
#: as measured by scripts/eval-tagger.py.
JUDGE_USD_PER_CALL = 0.002

#: What the excerpt Haiku reads holds at most, in characters: your
#: message, your message before it and the end of Claude's reply to that,
#: the end of Claude's final reply and each shell command; and how many
#: commands and changed files it names.
JUDGE_LIMITS = {
    "prompt": 2000, "previous": 300, "previous_reply": 600, "reply": 1500, "command": 100, "commands": 6, "files": 8,
}

#: Why a turn got no Haiku tag, as its line in :data:`JUDGE_DIR` says:
#: no ``claude`` command on the hook's path, it isn't signed in, Haiku took
#: too long, the call failed otherwise, or its answer held no tag.
JUDGE_ERRORS = ("no_cli", "no_login", "timeout", "failed", "no_tag")

JUDGE_INTRO = (
    "You label one exchange between a user and Claude, an AI coding assistant, for the user's own usage "
    "analytics. You get an excerpt of it: the user's message, what Claude did, and the end of Claude's final "
    "reply. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words. In "
    'them, "you" means Claude:'
)
#: The last line of what Haiku is told, in place of the note's
#: ``SKIP_KEY_LINE``: it sees an excerpt, not the work.
JUDGE_RULE = (
    "Judge only from the excerpt: a plan, skill or check it doesn't show wasn't there. Give every key that "
    "applies. Leave one out only when it doesn't fit this work (found outside research or a search; shift on "
    "a first message) or the excerpt can't tell at all."
)

#: What Haiku is told when it judges a finished agent run (the agent
#: metrics: ``result``, ``fit``, ``agent_brief``, ``retry``). A subagent
#: asked to end its report with a tag added it after a JSON-only answer,
#: breaking it, and the main session took the line for an injected
#: instruction; so no agent is asked for anything, and the capture hook's
#: ``SubagentStop`` entry hands Haiku an excerpt of the run instead.
AGENT_JUDGE_INTRO = (
    "You label one finished run of an AI coding agent, for the user's own usage analytics. You get an excerpt "
    "of it: the brief the agent was given, what it did, the end of its report and the session's earlier agent "
    "runs. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words:"
)
AGENT_JUDGE_RULE = "Judge only from the excerpt. Give every key; leave one out only when the excerpt can't tell at all."

#: What the agent excerpt holds at most, in characters: the brief, the
#: end of the report, each earlier run's brief and report end, and each
#: shell command; and how many earlier runs, commands and changed files
#: it names.
AGENT_JUDGE_LIMITS = {
    "brief": 2000, "report": 1500, "earlier": 4, "earlier_brief": 200, "earlier_report": 200, "command": 100,
    "commands": 6, "files": 8,
}

#: The agent metrics, each with the ``[cg: ...]`` keys Haiku answers for
#: it, in the order it is asked.
AGENT_JUDGE_KEYS = {"result": ("result",), "retry": ("retry",), "fit": ("fit",), "agent_brief": ("brief", "missing")}

#: The words each of those keys takes.
AGENT_JUDGE_VOCAB = {
    "result": RESULT_WORDS,
    "fit": TAG_VOCAB["fit"],
    "brief": TAG_VOCAB["brief"],
    "missing": ("files", "goal", "scope", "done", "none"),
    # "none" is how Haiku says a run is no retry; it is never kept.
    "retry": ("none", *RETRY_REASONS),
}

#: The hook that adds capture notes, the catalogue it reads and the module
#: it runs, installed side by side under ``<config-dir>/hooks/``. The
#: script Claude Code runs is a small launcher: Python keeps no bytecode
#: for a script it is started on, only for a module it imports, so the
#: work lives in :data:`HOOK_MODULE`, whose bytecode is kept between calls.
HOOK_SCRIPT = "capture-hook.py"
HOOK_MODULE = "capture_hook.py"
CATALOGUE_FILE = "capture-catalogue.json"


# -- coaching notes ------------------------------------------------------------

#: The marker every coaching note (``coaching_notes``) carries, then its
#: format version and the hint's kind (``cg-coach v1 quiet_output``). The
#: parser finds coaching notes by it. It differs from :data:`NOTE_MARKER`
#: so a coaching note never makes a session count as captured, whose
#: replies are then expected to carry tags.
COACH_MARKER = "cg-coach v"
#: The marker coaching notes carried until 0.12.1.
OLD_COACH_MARKER = "tl-coach v"
COACH_VERSION = 1

#: Your own split points and plan habit, worked out from your recent
#: sessions by the dashboard's service (``coaching.py``) for the hook to
#: read, in the data folder.
COACHING_FILE = "coaching.json"

#: What the hook keeps between calls, per session: which hint showed
#: when and how many times, the time, size and cache lifetime of the
#: newest reply (written by ``Stop`` and ``PostToolUse``, never any
#: words), and how far each subagent run had got. In the data folder.
COACH_STATE_FILE = "coach-state.json"

#: Tools whose results a coaching note may follow: every tool the
#: large-output note watches, and ``ExitPlanMode`` for an approved plan.
#: A settings.json written before the shell and MCP tools were dropped
#: still runs the hook after them until ``capture connect``; the hook
#: returns at once for any tool not named here.
COACHING_TOOLS = (*BIG_OUTPUT_TOOLS, "ExitPlanMode")

#: The live hints: after a tool result (the first three) and when you
#: send a message (the rest), most useful first when more than one
#: applies. ``drip_feed`` to ``big_paste`` are about how you prompt.
#: ``plan_fresh`` also applies when you send a go-ahead after a plan you
#: hadn't approved in the dialog, or leave plan mode. ``plan_fresh_early``
#: is the same advice at an earlier moment: a message sent in plan mode,
#: while there is still a plan to write, so it can end the plan the dialog
#: shows. ``cold_return`` is a receipt for a message sent after a break
#: that outlasted the prompt cache, and ``status_poll`` is for asking how
#: background work is going while it still runs. ``plan_fresh``,
#: ``plan_fresh_early`` and ``cold_return`` all say "start fresh", so they
#: share one rest stamp (``capture_hook.py``'s ``_FRESH_START_HINTS``).
COACHING_HINTS = (
    "plan_fresh",
    "plan_fresh_early",
    "split_run",
    "quiet_output",
    "drip_feed",
    "plan_first",
    "big_paste",
    "status_poll",
    "cold_return",
)

#: Hints that no longer show live. Their notes are still in old
#: transcripts, so the parser still knows the words and keeps those notes
#: as the hint they were instead of ``other``. ``clear_context`` is now a
#: row on the prompting section ("context carried into new pieces");
#: ``explore_reads`` became the habits page's "Explore cost by model"
#: table; ``repeat_ask``, ``stop_loop`` and ``vague_fix`` are counted after
#: the fact only (``prompting.py``), as live they fired on polls, refusals
#: and questions far more often than on the habit. ``cache_cold`` became
#: ``cold_return``: a receipt for every return after the cache expired,
#: where it had asked Claude to judge whether the message began new work.
RETIRED_COACHING_HINTS = (
    "clear_context",
    "explore_reads",
    "repeat_ask",
    "stop_loop",
    "vague_fix",
    "cache_cold",
)

#: The tip hints whose note asks for the tip only when Claude judges it
#: relevant to what the user asked ("if most of the message is a log").
#: Every other hint that carries a tip asks for it every time, so Claude
#: passing it on is a relay; for these, Claude showing it is a judgement,
#: and a tip it doesn't show is not a miss (``prompting``'s "Tips Claude
#: showed" counts the two differently).
CONDITIONAL_TIP_HINTS = ("big_paste",)

#: The hook events whose output reaches Claude or you (``additionalContext``
#: or ``systemMessage``). Claude Code runs a hook registered async without
#: waiting, and an async hook's output only arrives on the next turn, so
#: every entry for one of these is registered in the foreground
#: (:func:`hook_specs`, held by ``tests/test_footprint.py``). ``Stop`` is
#: not one: with coaching on it only writes the newest reply's time to
#: ``coach-state.json`` and prints nothing, so it runs in the background.
OUTPUT_EVENTS = ("SessionStart", "SubagentStart", "UserPromptSubmit", "PostToolUse")

#: Phrases that mark a message as correcting Claude ("that's wrong",
#: "still broken", "why did you", "undo that"), matched case-blind in a
#: message's first :data:`CORRECTION_SCAN_CHARS` characters. The parser
#: keeps only the yes/no (``Turn.human_correction``), which the report's
#: vague corrections (``prompting.py``) read. A bare "no" is deliberately
#: not a match: "no, go ahead" is as common as a correction.
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

#: Words that report a bad outcome, on top of :data:`CORRECTION_PATTERN`:
#: "it errors", "fails", "no change", "still not". A bare "fix this" or
#: "again" says nothing went wrong with the last attempt, so neither
#: matches. Only the report's ``vague_fix`` (``prompting.py``) uses it;
#: ``drip_feed`` goes by what Claude did, not by your words.
BAD_OUTCOME_PATTERN = (
    r"\b(?:broken|wrong|incorrect|bugs?|errors?|failing|fails|failed|crash(?:es|ed)?"
    r"|not (?:working|fixed|right)|no (?:change|difference|effect)"
    r"|still (?:the same|there|happening|not|no|fails?|failing|broken|wrong))\b"
)

#: A message that opens with a question word asks about fixes ("what
#: problems can you fix", "how do I fix the build"), not for one, so
#: ``vague_fix`` leaves it alone, as it does any message ending in "?".
#: Not "why": "why is it still broken" is the complaint the report is for,
#: unless it ends in a question mark.
QUESTION_PATTERN = r"\s*(?:what|which|who|whom|whose|where|when|how)\b"

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

#: Words that mark a message as adjusting Claude's work rather than
#: correcting it ("actually, make it blue", "rename it to X", "a bit
#: smaller"), matched case-blind in a message's first
#: :data:`CORRECTION_SCAN_CHARS` characters. Kept apart from
#: :data:`CORRECTION_PATTERN`, which would otherwise widen: a tweak
#: after a plan is not a complaint. A message that ends in a question
#: mark asks, so it never counts. The parser keeps only the yes/no
#: (``Turn.human_adjust``).
ADJUST_PATTERN = (
    r"(?:(?:^|[.!?]\s+)actually\b"
    r"|\b(?:instead|rather than|not quite|not exactly"
    r"|(?:change|set|turn|switch|swap|make) (?:it|that|this|them|these|those) (?:to|into)"
    r"|rename"
    r"|(?:should|needs? to|supposed to|meant to) (?:say|read|show|display|look)"
    r"|(?:a (?:bit|little)|slightly) (?:more|less|bigger|smaller|longer|shorter|wider|narrower|larger|taller"
    r"|higher|lower)"
    r"|too (?:big|small|long|short|wide|narrow|large|tall|bright|dark|loud|quiet|busy)"
    r"|move (?:it|that|this|them|these|those))\b)"
)

#: Words that mark a message as repeating something you already said
#: ("I told you", "as I said", "you didn't", "why did you"), matched
#: like :data:`ADJUST_PATTERN`. The parser keeps only the yes/no
#: (``Turn.human_remind``).
REMIND_PATTERN = (
    r"\b(?:"
    r"i (?:already |just )?(?:told you|said|asked|mentioned|wrote|specified)"
    r"|as i (?:said|mentioned|told you|asked|wrote)"
    r"|you (?:didn'?t|did not|never|forgot to|still haven'?t)"
    r"|why (?:did|didn'?t) you"
    r")\b"
)

#: A message that only tells Claude to carry on ("continue", "go ahead",
#: "do it", "implement the plan", "merge it", "yes, do it"), matched on
#: the whole message, at most :data:`GO_MAX_CHARS` characters. Sent
#: after a plan or a change, it asks for nothing new. The parser keeps
#: only the yes/no (``Turn.human_go``).
_GO_YES = r"yes|yep|yeah|yup|ok(?:ay)?|sure|agreed?|approved?|lgtm|looks good|sounds good"
_GO_CORE = (
    r"continue|keep going|carry on|proceed|go ahead|go on|go|do it|do that|go for it"
    r"|(?:implement|execute|apply|carry out|run|start|begin)(?: (?:the|this|that|my|your))? plan"
    r"|(?:merge|push|ship|deploy|commit)(?: (?:it|that|this|them))?(?: and tag)?"
)
GO_PATTERN = (
    rf"(?:please\s+)?(?:(?:{_GO_YES})(?:[\s,.!]+(?:{_GO_CORE}))?|(?:{_GO_CORE}))"
    r"(?:[\s,.!]+(?:please|now|thanks|thank you))*[\s!.,]*"
)
GO_MAX_CHARS = 60

#: A message that only asks how the work is going ("how is it going",
#: "what's the status", "is it done", "any updates", "progress?"),
#: matched on the whole message, under :data:`STATUS_MAX_CHARS`
#: characters. It asks for no change, so it's no new request. The parser
#: keeps only the yes/no (``Turn.human_status``).
_STATUS_CORE = (
    r"how(?:'?s| is| are)(?: it| this| that| everything| things| the [a-z]+(?: [a-z]+)?)?"
    r" (?:going|progressing|looking|coming(?: along)?|doing)"
    r"|what(?:'?s| is)(?: the)? (?:status|progress|state|remaining|left|happening|going on)"
    r"|(?:is|are)(?: it| this| that| everything| they| we)? (?:all )?(?:done|finished|complete|completed|merged"
    r"|running|ready|working|still (?:running|working|going))"
    r"|are you (?:still )?(?:working|running|there|done|finished)"
    r"|any (?:updates?|progress|news|findings|results?)"
    r"|where are we(?: at| now)?"
    r"|status|progress|updates?"
    r"|how (?:long|much) (?:is )?(?:left|remaining|to go)"
)
STATUS_PATTERN = (
    rf"(?:(?:hi|hey|ok|okay|so|and|well)[\s,.!]+)?(?:{_STATUS_CORE})"
    r"(?:[\s,.!?]+(?:now|yet|so far|please|there|at the moment|currently))*[\s!.,?]*"
)
STATUS_MAX_CHARS = 120

#: What a tool's result says when the work it started went to the
#: background, so Claude carries on without it: a shell command started
#: with ``run_in_background`` or moved there after its timeout, an agent
#: launched in the background, a workflow. Read in memory, in the first
#: :data:`BACKGROUND_SCAN_CHARS` characters of the result's text, by the
#: hook's ``status_poll``; the text is dropped at once. The result's own
#: wording is matched, not the call's ``run_in_background``, which an agent
#: or a workflow sent to the background without one.
BACKGROUND_LAUNCH_PATTERN = (
    r"Command running in background with ID: "
    r"|was moved to the background \(ID: "
    r"|Async agent launched successfully"
    r"|Workflow launched in background\. Task ID: "
)
BACKGROUND_SCAN_CHARS = 400

#: How Claude Code records that you stopped a reply (Esc).
INTERRUPT_PREFIX = "[Request interrupted"

#: What the feedback you give a rejected plan sounds like, as closed
#: words, tried in this order: you're unsure, you ask something, you point
#: at something wrong or to change, or none of those. The parser keeps
#: only the word (``PlanStats.feedback_class``) and the message's length.
#: Read in memory by ``prompt_shape.plan_feedback_class``; the hook never
#: needs them, so they stay out of the exported catalogue.
PLAN_FEEDBACK_CLASSES = ("question", "critique", "unsure", "other")
PLAN_FEEDBACK_SCAN_CHARS = 400
PLAN_UNSURE_PATTERN = (
    r"\b(?:not (?:really )?sure|unsure|not (?:fully )?convinced|not certain|i don'?t know|no idea|i wonder"
    r"|on the fence|torn|second thoughts?|let me think|hmm+)\b"
)
PLAN_QUESTION_PATTERN = (
    r"\s*(?:what|which|who|whom|whose|where|when|why|how|can|could|would|should|do|does|did|is|are|will|isn'?t"
    r"|aren'?t|shouldn'?t|won'?t)\b"
)
PLAN_CRITIQUE_PATTERN = (
    r"\b(?:no|not|don'?t|do not|doesn'?t|shouldn'?t|never|wrong|instead|rather|too|remove|drop|skip|without"
    r"|avoid|but|change|only|also|needs? to|must|should)\b"
)

#: How a reply's closing question is found (``prompt_shape.ends_on_question``,
#: which the capture hook and the status line repeat step for step). The
#: reply loses its fenced code (an unclosed fence runs to the end), a
#: ClaudeGlass quote block (a tip, or the feedback reminder with the lines
#: of quote after it), inline code, URLs, the reminder as a bare line, a
#: question inside quotation marks and the ``[cg: ...]`` tag that ends it.
#: What is left is cut into sentences and list items at
#: :data:`REPLY_UNIT_PATTERN`, and only its last :data:`REPLY_SCAN_CHARS`
#: characters are read: a question mark must close one of the last two
#: sentences, or one of the list items that end it. A closing bracket,
#: quote or emphasis mark (:data:`REPLY_QUESTION_TRIM`) may follow the
#: question mark. A question mark in the middle of a paragraph, or a
#: rhetorical one many sentences back, doesn't count.
REPLY_SCAN_CHARS = 1_200
REPLY_FENCE_PATTERN = r"(?s)```.*?(?:```|\Z)"
REPLY_TIP_BLOCK_PATTERN = r"(?m)^[ \t]*>[ \t]*(?:\U0001F4A1\U0000FE0F?[ \t]*)?[*_]*ClaudeGlass(?: tip)?:.*(?:\n[ \t]*>.*)*\n?"
REPLY_INLINE_CODE_PATTERN = r"`[^`\n]*`"
REPLY_URL_PATTERN = r"https?://\S+|www\.\S+"
REPLY_QUOTED_PATTERN = r"[\"\U0000201C][^\"\U0000201C\U0000201D\n]{0,120}[\"\U0000201D]"
REPLY_REMINDER_LINE_PATTERN = r"(?m)^.*Finished\? Run /(?:cg|tl)-feedback.*\n?"
REPLY_TAGS_PATTERN = r"(?i)(?:\[(?:cg|tl|result|cg-fb|tl-fb):[^\[\]\n]{0,400}\][`*_.\s]*){1,3}\s*$"
REPLY_LIST_START_PATTERN = r"^\s*(?:[-*+\U00002022]|\d{1,3}[.)])\s+"
REPLY_UNIT_PATTERN = r"(?<=[.!?])\s+|\n+"
REPLY_QUESTION_TRIM = "*_`\"')]>~\u201d\u2019"

#: What Claude says when it owns a mistake: first person and past tense
#: ("I was wrong", "my mistake", "I misread", "I got it wrong", "I should
#: have checked", "I didn't run", "you're right", "that was wrong"),
#: matched case-blind in the first :data:`ADMIT_SCAN_CHARS` characters of
#: a reply's text block, with its code, URLs and a ClaudeGlass quote block
#: left out. "Good catch" and "fair point" aren't here: they thank you for
#: the catch without owning anything, so they count only through an
#: admission that follows ("Good catch, I missed that"). A candidate, not a
#: verdict: about 60 in 100 are real, and the capture tag's ``admit`` word
#: confirms one.
#: The parser keeps only the yes/no (``Turn.admit_candidate``).
ADMIT_PATTERN = (
    r"\b(?:"
    r"i(?:'m| am| was) (?:wrong|mistaken|incorrect)"
    r"|i (?:got|had|made) (?:that|this|it|them|those|these)(?: all)? (?:wrong|incorrect)"
    r"|i made (?:a|an|that|this|the same) (?:mistake|error)"
    r"|my (?:mistake|bad|error|fault|oversight|misreading|misunderstanding)"
    r"|that was (?:wrong|incorrect|a mistake|my (?:mistake|error|fault))"
    r"|i mis(?:read|understood|stated|spoke|counted|named|labell?ed|reported|judged|interpreted|remembered|quoted"
    r"|described)"
    r"|i (?:incorrectly|wrongly|mistakenly) \w+"
    r"|i(?:'d| had) (?:missed|overlooked|forgotten|misread|misunderstood)"
    r"|i should(?:n'?t| not)? have (?!(?:a|an|the|some|more|any|no|enough)\b)"
    r"|i (?:didn'?t|did not|failed to|forgot to|neglected to) "
    r"(?:follow|read|check|run|apply|use|verify|test|update|include|do what|honou?r|respect|account)"
    r"|i (?:hadn'?t|had not) (?:checked|run|read|verified|tested|looked|considered|accounted)"
    r"|i (?:ignored|overlooked|missed|forgot) (?:that|this|it|your|the|to)"
    r"|you(?:'re| are) (?:absolutely |completely |totally |quite |entirely )?right"
    r"|i apologi[sz]e for (?:the |my |that |this )?(?:mistake|error|oversight|mix-?up|confusion)"
    r"|sorry (?:about that|for the (?:mistake|error|confusion|mix-?up)|,? (?:i|that was|my))"
    r")\b"
)
ADMIT_SCAN_CHARS = 600

#: What Claude says when it disowns a ClaudeGlass tip it was given: the
#: tip "misfired", was a false positive or alarm, "doesn't apply" or is
#: not relevant. Counts only in a reply that carries a tip, within
#: :data:`MISFIRE_NEAR_CHARS` characters of the word "ClaudeGlass" in the
#: reply's own prose (not the tip's quote block). The parser keeps only
#: the yes/no (``Turn.tip_disowned``).
MISFIRE_PATTERN = (
    r"\b(?:mis-?fire[sd]?|false (?:positive|alarm)|(?:does|do|did)(?:n'?t| not) (?:really |actually )?apply"
    r"|(?:is |was )?not (?:really )?(?:applicable|relevant)|isn'?t (?:really )?(?:applicable|relevant))\b"
)
MISFIRE_NEAR_CHARS = 120

#: A message of yours that asks something, for ``Turn.admit_caught`` and
#: ``Turn.human_question``: it ends in a question mark, or opens with a
#: question word.
ASKS_PATTERN = r"\s*(?:what|which|who|whom|whose|where|when|why|how)\b"

#: The desktop app's resume ping after a usage limit, and its note after
#: you quit it mid-reply: a human-looking line you didn't type.
LIMIT_RESUME_PREFIX = "I hit my usage limit while you were working, but it has reset now"
APP_QUIT_PREFIX = "The app was quit while you were working"

#: What a background task's finishing message starts with, however Claude
#: Code writes it: as a user line, a queued command or a queue operation.
TASK_NOTIFICATION_PREFIX = "<task-notification"

#: What a line written as your message starts with when you didn't type
#: it: a slash command and its output, a ``!`` shell command, a
#: scheduled task, a background agent's report, a message from another
#: session, the app's own resume pings. The one list the parser, the
#: hook and the status line share, so none of them hands such a line a
#: hint or counts it as one of your messages.
NOT_TYPED_PREFIXES = (
    "<command-", "<local-command-", "<bash-", "<scheduled-task", "<<autonomous-loop", TASK_NOTIFICATION_PREFIX,
    "[SYSTEM NOTIFICATION", "<agent-message", "<cross-session-message", "Another Claude session sent a message",
    LIMIT_RESUME_PREFIX, APP_QUIT_PREFIX,
)

#: ``turnOrigin`` values that rule a line out as yours. Never the other
#: way: 38 scheduled tasks and 4 resume pings carry ``human``, so
#: ``human`` proves nothing. ``sdk`` is left out: a session driven by
#: ``claude -p`` has no other request than its own prompt.
NOT_TYPED_TURN_ORIGINS = ("task_notification", "peer", "scheduled")

#: How a shell line shows that it runs a project's tests. ``testrun`` (the
#: parser, the purpose rules) and the capture hook read the same pieces,
#: so the two always agree on what counts. A heredoc's body is dropped
#: first (:data:`TEST_HEREDOC_PATTERN`: a script or commit message that
#: mentions pytest runs nothing); the line is cut into commands at
#: :data:`TEST_COMMAND_SPLIT_PATTERN`; each command loses what comes before
#: its program (:data:`TEST_PREFIX_PATTERN`: a "(" or "&", a ``VAR=value``,
#: ``time``, ``timeout 60``, ``uv run``, ``npx``) and the folder and
#: ``.exe`` of its program word (:data:`TEST_PROGRAM_PATTERN`, so
#: ``C:/Python311/python.exe`` and ``.venv/Scripts/pytest`` read as
#: ``python`` and ``pytest``); what is left must open with a runner
#: (:data:`TEST_RUNNER_PATTERN`), unless its arguments say nothing is run
#: (:data:`TEST_NO_RUN_PATTERN`: ``--collect-only``, ``--help``). A path in
#: a hook's text is redacted to ``<path>``, so that counts as a Python too.
TEST_COMMAND_SPLIT_PATTERN = r"&&|\|\||;|\||\r?\n|[)}]"
TEST_HEREDOC_PATTERN = (
    r"(?s)(?<!<)<<(?!<)-?[ \t]*(?P<quote>['\"]?)(?P<tag>[A-Za-z_]\w*)(?P=quote)(?P<rest>[^\n]*)\n"
    r".*?(?:\n[ \t]*(?P=tag)[ \t]*(?=\n|\Z)|\Z)"
)
TEST_PREFIX_PATTERN = (
    r"(?:[(&{]\s*"
    r"|(?:\$env:)?\w+=\S*\s+"
    r"|(?:time|nohup|command|exec|sudo|do|then|else|elif|if|while|until|!)\s+"
    r"|timeout(?:\.exe)?\s+(?:-\S+\s+)*\d+[smhd]?\s+"
    r"|(?:uv|poetry|pipenv|pdm|hatch|rye)\s+run\s+(?:--[\w-]+\s+)*"
    r"|(?:npx|bunx)\s+(?:(?:-y|--yes|--no-install)\s+)?"
    r"|(?:pnpm|yarn)\s+(?:exec|dlx)\s+"
    r"|bundle\s+exec\s+)"
)
TEST_PROGRAM_PATTERN = (
    r"""^(?:"(?:[^"\n]*[/\\])?|'(?:[^'\n]*[/\\])?|(?:[^\s"']*[/\\])?)([\w.+-]+?)(?:\.(?:exe|cmd|bat))?["']?(?=\s|$)"""
)
TEST_RUNNER_PATTERN = (
    r"(?:(?:python3?(?:\.\d+)?|py|<path>)(?:\s+(?:-[uBEsSqIOd]+|-[XW]\s*\S+|-\d(?:\.\d+)?))*\s+-m\s+(?:pytest|unittest)"
    r"|pytest|py\.test|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest"
    r"|(?:npm|yarn|pnpm|bun)(?:\s+run)?\s+test(?::[\w-]+)?"
    r"|(?:go|cargo|dotnet|deno|swift|mix|flutter|dart|rake)\s+test|cargo\s+nextest\s+run"
    r"|mvnw?\s+test|gradlew?\s+test|playwright\s+test|make\s+test)"
)
TEST_NO_RUN_PATTERN = (
    r"(?:^|\s)(?:--collect-only|--co|--help|-h|--version|--fixtures|--markers|--setup-plan|--listTests|--list-tests"
    r"|--no-run)(?=\s|$)"
)

#: What in a test command's arguments picks particular tests: a name or
#: path filter, a ``file::test`` id, a test file or folder, or (go, cargo:
#: :data:`TEST_BARE_TARGET_RUNNER_PATTERN`) a package or test name. A
#: redirection and the value of a flag like ``-n 4`` or ``--cov src`` pick
#: nothing (:data:`TEST_NO_TARGET_PATTERN`), nor does naming the whole
#: suite: ``tests``, ``./...``, ``.`` (:data:`TEST_WHOLE_SUITE_PATTERN`).
TEST_TARGET_PATTERN = (
    r"(?:^|\s)(?:-k\b|-t\b|--testNamePattern|--testPathPattern|-run\b|--grep\b|--filter\b|--tests\b|-Dtest"
    r"|\S*::\S+|\S*tests?/\S*|\S*test_\S+|\S+_test\.\w+|\S+\.(?:test|spec)\.\w+|\S*spec/\S*)"
)
TEST_BARE_TARGET_PATTERN = r"(?:^|\s)(?!-)[\w./:-]+"
TEST_BARE_TARGET_RUNNER_PATTERN = r"(?:go|cargo)\s+test"
TEST_NO_TARGET_PATTERN = (
    r"(?:\s*&?\d?>>?&?\s*\S*"
    r"|(?:^|\s)(?:-n|-p|-c|-j|-o|-W|--cov(?:-report|-config)?|--maxfail|--tb|--timeout|--durations|--rootdir"
    r"|--junitxml|--workers|--shard|--reporter|--config|--maxWorkers)(?:\s+|=)\S+)"
)
TEST_WHOLE_SUITE_PATTERN = r"(?:^|\s)(?:\./\.\.\.|(?:\./)?(?:tests?|specs?|__tests__|src|\.)/?)(?=\s|$)"

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

#: A path inside a ``.claude`` folder: memory, plans, workflow scripts, and
#: a project's agents, skills and settings. Claude changing one is not
#: work on your project, so a message answered only with those is no
#: change request for ``drip_feed``. The parser counts them apart
#: (``Turn.config_edit_count``), the hook and status line read this.
CONFIG_PATH_PATTERN = r"(?:^|[\\/])\.claude(?:[\\/]|$)"

#: What a shell command that changes a file looks like, for the hook, which
#: can't read commands the way ``shell_writes`` does: an in-place ``sed``
#: or ``perl``, PowerShell's ``Set-Content``/``Add-Content`` and
#: ``[IO.File]::WriteAll*``, and a ``>``, ``>>`` or ``tee`` fed by ``cat``,
#: ``echo`` or ``printf`` (or, in PowerShell, a string, a variable,
#: ``Get-Content`` or ``echo`` piped to ``Out-File``/``Tee-Object``). Not a
#: program's output sent to a log, a stderr redirection or a device. An
#: approximation of ``shell_writes.write_targets`` on whole commands, held
#: to it by a test over a corpus of commands.
SHELL_WRITE_PATTERN = (
    r"(?:\b(?:sed|perl)\b[^\n|;&]*?\s-(?:[A-Za-z]*i[A-Za-z]*(?=[\s.]|$)|-in-place\b)"
    r"|\b(?:Set|Add)-Content\b"
    r"|\[(?:System\.)?IO\.File\]::(?:WriteAll(?:Text|Lines|Bytes)|AppendAll(?:Text|Lines))"
    r"|\b(?:cat|echo|printf)\b[^\n|;&]*?(?<![\d&])>>?(?!&)[ \t]*(?!/dev/null|nul\b|\$null)[^\s&|;>]"
    r"|\b(?:cat|echo|printf)\b[^\n]*\|[ \t]*tee\b[ \t]+(?:-a[ \t]+)?(?!/dev/null|nul\b)[^\s&|;>-]"
    r"|(?:^|[;\n(&|])[ \t]*(?:[\"'$@]|(?:echo|write-output|write|get-content|gc|cat|type)\b)[^\n;]*"
    r"\|[ \t]*(?:out-file|tee-object)\b)"
)

#: A message that opens by asking Claude to look, not to change anything
#: ("review the diff", "explain how X works", "can you check Y"). A job
#: that opens like this is not one to plan before it starts
#: (``plan_first``).
REVIEW_PATTERN = (
    r"\s*(?:please\s+)?(?:(?:can|could|would) you\s+(?:please\s+)?)?(?:review|audit|check|look (?:at|over|through|into)"
    r"|read|explain|summari[sz]e|analy[sz]e|investigate|inspect|explore|compare|assess|evaluate|critique|list|show"
    r"|tell me|describe|walk me through|go through|examine|proofread|verify)\b"
)

#: What isn't your own prose in a message you typed: a fenced block (or
#: one left open), a quoted ("> ") line, and a pasted log or stack-trace
#: line (a date or time, a log level, "Traceback", ``File "``, ``at f (``).
#: ``plan_first`` counts the changes you ask for in what's left.
PROSE_NOISE_PATTERN = (
    r"(?s:```.*?(?:```|\Z))|(?m:^[ \t]*>.*$)"
    r"|(?m:^[ \t]*(?:\[?\d{4}-\d\d-\d\d|\d\d:\d\d:\d\d|(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL)\b"
    r"|Traceback\b|File \"|at \S+ \().*$)"
)

#: A message that is a plan already, so it needs none first: a heading
#: ("# Plan", "**Steps**") in prose of at least :data:`PLAN_DOC_CHARS`
#: characters, or :data:`PLAN_DOC_ITEMS` numbered items.
PLAN_HEADING_PATTERN = r"(?m)^[ \t]*(?:#{1,6}[ \t]+\S|\*\*[^*\n]{2,80}\*\*:?[ \t]*$)"
PLAN_NUMBERED_PATTERN = r"(?m)^[ \t]*\d{1,2}[.)][ \t]+\S"
PLAN_DOC_CHARS = 1_500
PLAN_DOC_ITEMS = 5

#: What the after-the-fact report counts for the habits that no longer
#: show a live hint (``vague_fix``, ``repeat_ask``, ``stop_loop``): the
#: parser and ``prompting.py`` read these, the hook never does, so they
#: are not in ``[thresholds]``.
REPORT_THRESHOLDS = {
    #: A correction this short, naming nothing specific, is vague.
    "vague_fix_chars": 80,
    #: A message sharing this much of its words with one you sent...
    "repeat_similarity": 0.8,
    #: ...within this long, is the same request again...
    "repeat_window_minutes": 60,
    #: ...when it has at least this many different words.
    "repeat_min_words": 4,
    #: Stopping Claude this many times...
    "stop_loop_count": 3,
    #: ...within this long is a loop of stops.
    "stop_window_minutes": 20,
}

#: When each hint applies, and how often it may repeat. Each can be
#: changed in ``config.toml``'s ``[thresholds]`` as ``coaching_<key>``.
COACHING_THRESHOLDS = {
    #: The smallest context the cold-return receipt is worth mentioning:
    #: below it, rewriting the cache costs cents.
    "cold_min_tokens": 100_000,
    #: How long the cold-return receipt rests once shown, in hours, however
    #: much the context has grown: one a break is plenty.
    "cold_rest_hours": 12,
    #: The tokens every session starts with and shares (the system prompt
    #: and the tools), about 40 to 43k. The cold-return receipt leaves them
    #: out, as a reply rewrites that part for everyone, break or not.
    "warm_prefix_tokens": 42_000,
    #: A tool result this many tokens long gets the narrower-output hint.
    "quiet_output_tokens": 8_000,
    #: Planning context, in tokens, kept after an approved plan before the
    #: fresh-session hint applies (``plan_handoff_min_dropped_tokens``'s
    #: default).
    "plan_fresh_tokens": 40_000,
    #: This many small requests in a row, each of which Claude answered by
    #: changing files outside a ``.claude`` folder, get the
    #: plan-it-as-one-prompt hint. A go-ahead, a thank-you, a status check
    #: or a question is no request and neither counts nor ends the run...
    "drip_count": 3,
    #: ...when each was sent within this long of Claude's reply before it.
    "drip_window_minutes": 20,
    #: A message longer than this isn't a small request.
    "drip_chars": 300,
    #: A message this many tokens long gets the big-paste hint.
    "big_paste_tokens": 10_000,
    #: A request asking for this many separate changes (edits, counted in
    #: your own prose only), outside plan mode, and not already a plan or a
    #: request to review, gets the plan-first hint...
    "plan_steps": 3,
    #: ...when it's at least this long.
    "plan_min_chars": 150,
    #: A hint that showed stays quiet this long in the same session (twice
    #: as long after the second time, and so on, up to ``max_backoff`` times
    #: this)...
    "cooldown_minutes": 30,
    #: ...unless what's at stake has grown this many times since.
    "rearm_factor": 1.5,
    #: The most times a hint's rest may double.
    "max_backoff": 3,
}

#: What the prompting hints say about the work itself: nothing. They're
#: about how the user prompts, so Claude does the work as it would have
#: and only adds the tip; telling it to plan first, ask first, wait for a
#: go-ahead or change its approach steered the work itself.
_AS_USUAL = "Handle the message exactly as you would have without this note: it changes nothing about the work."

#: What each tip says, word for word. A tip reaches you through Claude's
#: reply, the one place every app shows (the desktop app folds a hook's
#: ``systemMessage`` into a collapsed row), so the note's *last line* is
#: the tip itself, behind :data:`TIP_LABEL`, for Claude to copy. Where
#: Claude Code shows hook messages, :data:`COACHING_NOTICE` shows the same
#: words at once. One line each, so the note ends on the tip.
#: ``{placeholders}`` are filled from the session (see
#: :data:`COACHING_TEXT`) and, for how to do something in the app you use,
#: from :data:`COACHING_HOW`. Written to the user: "you" is the user.
COACHING_TIP = {
    "plan_fresh": (
        "This plan was approved with about {kept} tokens of planning in the session, and every reply of the "
        "build reads them again. Building it in a fresh session would carry about {kept} fewer tokens on each "
        "reply. {fresh_how}"
    ),
    "plan_fresh_early": (
        "Approve with clear context: this session holds about {kept} tokens of planning chat, and every reply of "
        "the build would read it again. {clear_how}"
    ),
    "cold_return": (
        "The prompt cache expired while this session sat idle for {idle}, so this reply wrote about {ctx} tokens "
        "of context again{compacted}. If you came back only to see whether the work is done, the last reply or "
        "the task panel already says. For new work, running /clear first skips the rewrite."
    ),
    "status_poll": (
        "Asking how it's going while work runs in the background makes Claude read the whole session, about "
        "{ctx} tokens, to say little that is new. {poll_how}"
    ),
    "drip_feed": (
        "That's {count} small changes in a row, each its own message, and each one re-reads the whole session, "
        "about {ctx} tokens. Working out everything the work still needs and sending it as one message gets it "
        "done in one pass, for fewer tokens."
    ),
    "plan_first": (
        "That's a job of about {steps} separate changes, sent outside plan mode. Plan mode agrees the approach "
        "before anything changes. {plan_how}"
    ),
    "big_paste": (
        "Your message is about {tokens} tokens, and every later reply reads it again. Pasting only the part that "
        "matters, or saving the rest to a file and giving the path, costs less."
    ),
}


def _tip_line(hint: str) -> str:
    return f"{TIP_LABEL} {COACHING_TIP[hint]}"


#: A tip note's first sentence: write the tip. Where Claude is to judge
#: first whether it applies (:data:`CONDITIONAL_TIP_HINTS`), the condition
#: leads, and the sentence still ends in the order to write it.
_WRITE_TIP = "write the tip on this note's last line, word for word, {where}."
_AT_END = "at the end of your reply, after a blank line and before any tag"
_BEFORE_BUILD = "before you start building"
_ENDING_THE_PLAN = "as the last line of the plan you submit for approval"


def _tip_note(hint: str, *, when: str = "", then: str = "", where: str = _AT_END) -> str:
    """The note for a hint that passes a tip on: a first sentence saying
    to write it (after ``when``, for a conditional one), what Claude
    needs to know, then the tip as the last line."""
    write = _WRITE_TIP.format(where=where)
    first = f"If {when}, {write}" if when else write[0].upper() + write[1:]
    return "\n".join((" ".join(part for part in (first, then) if part), _tip_line(hint)))


#: How to do what a tip suggests, by the app the hook runs in: ``desktop``
#: is the desktop app's Code tab (``CLAUDE_CODE_ENTRYPOINT`` is
#: ``claude-desktop``), ``terminal`` is everywhere else. The hook fills the
#: four placeholders of the tips above from these: ``{plan_how}`` (start
#: plan mode), ``{fresh_how}`` (after a plan was approved: the next time
#: clears the context at approval), ``{clear_how}`` (before it's
#: approved) and ``{poll_how}`` (where to see what a background task is
#: doing without asking). The desktop app has no Shift+Tab for plan mode, and its
#: approval dialog has an option that clears the context; where there's no
#: such option, ``/clear`` and asking for the saved plan does the same.
#: ``<file>`` stays as written, in backticks so a markdown view doesn't
#: read it as a tag: a note never carries a path.
COACHING_HOW = {
    "plan_how": {
        "desktop": "Start the message with /plan, or pick Plan in the mode menu next to Send.",
        "terminal": "Press Shift+Tab to switch to it.",
    },
    "fresh_how": {
        "desktop": (
            "Next time, pick the approval option that clears the context first; if the dialog has none, run "
            "/clear, then ask Claude to implement the plan in `<file>`."
        ),
        "terminal": "Run /clear, then ask Claude to implement the plan in `<file>`.",
    },
    "clear_how": {
        "desktop": (
            "Pick the approval option that clears the context first; if the dialog has none, run /clear, then ask "
            "Claude to implement the plan in `<file>`."
        ),
        "terminal": "Run /clear first, then ask Claude to implement the plan in `<file>`.",
    },
    # Where to look instead of asking: the desktop app's task panel, or the
    # terminal's /tasks list. Neither promises a notification: a background
    # run only sends one when it finishes, and not every kind does.
    "poll_how": {
        "desktop": "The task panel shows what is still running, with no message sent.",
        "terminal": "Typing /tasks shows what is still running, with no message sent.",
    },
}

#: What each hint asks of Claude; ``""`` for one that only shows you a
#: notice. A hint that passes a tip on starts with the order to write it
#: and ends with the tip itself (see :data:`COACHING_TIP`). ``{placeholders}``
#: are filled from the session: token counts in thousands (``150k``), an
#: idle time, a count. A note never carries a path, a command or your
#: words.
COACHING_TEXT = {
    "cold_return": _tip_note(
        "cold_return", then="The user came back to this session after a break. " + _AS_USUAL
    ),
    "status_poll": _tip_note(
        "status_poll",
        then="The user is asking about work that is still running in the background. " + _AS_USUAL,
    ),
    "drip_feed": _tip_note(
        "drip_feed", then="The user has sent {count} small change requests in a row, one message each. " + _AS_USUAL
    ),
    "plan_first": _tip_note(
        "plan_first",
        then="The user's message asks for about {steps} separate changes, outside plan mode. " + _AS_USUAL,
    ),
    "big_paste": _tip_note(
        "big_paste",
        when="most of the user's message is a log, a file or command output",
        then="Otherwise don't mention this note.",
    ),
    "quiet_output": "That result was about {tokens} tokens, and every later reply reads it again. Next time, {how}.",
    "plan_fresh": _tip_note(
        "plan_fresh", where=_BEFORE_BUILD, then="Then carry on unless the user stops you."
    ),
    "plan_fresh_early": _tip_note(
        "plan_fresh_early",
        where=_ENDING_THE_PLAN,
        then="If this reply doesn't end in a plan, add it to the plan you submit later. " + _AS_USUAL,
    ),
    # Nothing reaches the subagent: told mid-run to stop and hand back,
    # it either ignored the note (and reported it as a stray hook message)
    # or would have handed back half-done work. You get the notice instead.
    "split_run": "",
}

#: The hints that also show the tip as a notice where Claude Code shows
#: hook messages: every hint with a tip.
NOTICE_HINTS = (
    "plan_fresh", "plan_fresh_early", "drip_feed", "plan_first", "big_paste", "status_poll", "cold_return",
)

#: What the hook shows you itself, never sent to Claude, so it costs no
#: tokens: Claude Code's hook ``systemMessage``. The desktop app shows it
#: as a collapsed row that folds into the run summary, and never for a
#: subagent, so there the tip reaches you through Claude alone and the
#: hook sends no notice (``capture_hook.py``'s ``delivery``). Elsewhere it
#: shows at once, with the same words as the tip. ``split_run`` shows it
#: once, when a subagent run passes its split point, and tells the
#: subagent nothing: with no note to Claude, the notice is all it has.
#: Same ``{placeholders}`` as :data:`COACHING_TEXT`.
COACHING_NOTICE = {
    **{hint: NOTICE_LABEL + COACHING_TIP[hint] for hint in NOTICE_HINTS},
    "split_run": NOTICE_LABEL + "this {agent} run has made about {replies} replies, and each one reads the whole "
    "run again. In your past sessions {agent} runs cost less when split about every {every_n} replies: next "
    "time, give each agent a smaller piece of the work.",
}

#: ``quiet_output``'s ``{how}``, by tool; ``""`` for any other tool.
COACHING_QUIET_HOW = {
    "Read": "read only the lines you need, with an offset and a limit",
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
    #: The line explaining its key in the main session's ``[cg: ...]``
    #: tag; and, for an agent metric, the line Haiku gets for its keys
    #: when it judges an agent run (:func:`agent_judge_text`).
    main_line: str = ""
    sub_line: str = ""
    #: A line of its own in the main or subagent note.
    main_extra: str = ""
    sub_extra: str = ""
    #: Put ``main_extra`` before the ``[cg: ...]`` tag block instead of
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
        what="Whether each subagent finished its task, finished part of it, or was blocked, judged by Claude "
        "Haiku from its brief and the end of its report once it's done.",
        why="Which agents and models deliver, and which get re-run.",
        powers=("delegation", "models", "outcome"),
        hooks=("SubagentStop",),
        sub_line="result: done|partial|blocked (done = the agent finished what its brief asked; partial = some "
        "of it; blocked = it could not go on, such as a missing file, tool or permission)",
    ),
    Metric(
        id="retry",
        group="essentials",
        section="subagents",
        title="Why an agent was run again",
        what="When an agent run redoes an earlier one in the session that fell short, the reason: model, "
        "brief, tools, scope or other, judged by Claude Haiku from the two runs.",
        why="Why agents are re-run, and a guard that stops ClaudeGlass suggesting a cheaper model for work "
        "that needed a stronger one.",
        powers=("delegation", "models"),
        hooks=("SubagentStop",),
        # A choice that includes none: asked for the key only when it
        # applied, Haiku left it out of plain retries (a tools retry 1 time
        # in 3); scripts/eval-agent-judge.py measures it (docs/tagger-eval.md).
        sub_line="retry: none|model|brief|tools|scope|other (is this run a second try at what an earlier run listed "
        "below was meant to deliver, because that run fell short? none = no, or there is no earlier run; model = it "
        "needed a stronger model; brief = the earlier brief was too vague or lacked something; tools = the earlier "
        "agent lacked a tool or a permission; scope = the task was cut too wide or changed; other)",
        requires=("result",),
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
        what="Whether a smaller model would have done each subagent's task, or it needed a larger one, judged "
        "by Claude Haiku once the run is done.",
        why="Agent model tuning. Used only to rule a cheaper model out, never to recommend one.",
        powers=("models", "delegation"),
        hooks=("SubagentStop",),
        sub_line="fit: smaller|right|larger (smaller = a smaller, cheaper model could plainly have done this: "
        "simple lookups or mechanical edits; right = it suited the model it ran on; larger = the agent "
        "struggled in a way a stronger model would not have)",
        requires=("result",),
    ),
    Metric(
        id="agent_brief",
        group="standard",
        section="subagents",
        title="Agent brief quality",
        what="How complete each subagent's brief was, and what it lacked: files, goal, scope or what done "
        "means, judged by Claude Haiku from the brief and the run.",
        why="Brief quality per agent type, and better agent definitions and brief templates.",
        powers=("delegation", "information"),
        hooks=("SubagentStop",),
        sub_line="brief: clear|partial|vague (how complete the brief was: clear = what to do and what to hand "
        "back; partial = the goal without the details; vague = neither); missing: files,goal,scope,done or none "
        "(what the brief lacked that the agent had to find or guess; a comma list)",
        requires=("result",),
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
        what=f"After a read, search or web result of about {BIG_OUTPUT_TOKENS:,} tokens or more, how much of it "
        "Claude needed: all, part or none. Only what Claude reads counts. "
        f"A picture counts for at most {RESULT_IMAGE_MAX_TOKENS:,} tokens. A result Claude Code saved to a file "
        "counts for its preview alone. Shell and MCP results are not asked about. Claude Code waits for the hook "
        "after each read, search or web result. 'claudeglass capture status' shows how long that has added, measured from your own "
        "sessions.",
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
        "done-criteria wording or numbered steps. Also whether it's short, tweaks earlier work, repeats "
        "something you said, only says to carry on, or only asks how it's going. Messages you typed "
        "while Claude was working are read the same way, and counted. Only yes/no and counts are kept.",
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
        what="Each plan you approved or rejected: steps, files named and length. For a rejected plan, how long "
        "your feedback was and one word for how it reads: a question, a criticism or unsure. Whether you "
        "approved it by typing a go-ahead or by leaving plan mode.",
        why="Plan size against what the work then cost, and how many rounds a plan took before you approved it.",
        powers=("planning",),
    ),
    Metric(
        id="tool_denials",
        group="derived",
        section="derived",
        title="Tool calls turned away",
        what="Why each tool call didn't run, as one word. A plan or question you answered, a hook or auto "
        "mode block, a closed dialog, or a call you turned down. Also how many clarifying questions Claude "
        "asked you. Only the word and the count are kept.",
        why="Counting only the calls you turned down when judging how often you stop Claude, not plan dialogs "
        "or hooks.",
        powers=("waiting", "planning"),
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
        what="The same command failing again and again in one piece of work. It is counted on the Savings page, "
        "with the commands that failed; no hint speaks up while it happens.",
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
        what="A second status line with a live hint from your session. For example, a cache about to expire, "
        "a large last output, or small requests sent one at a time.",
        why="Advice where you work, at the moment it applies. The status line is never sent to Claude.",
        powers=("context", "tool_output"),
    ),
    Metric(
        id="coaching_notes",
        group="coaching",
        section="coaching",
        title="Coaching notes from Claude",
        what="Live hints for where the status line doesn't show, such as the desktop app. When one applies, a "
        "hook adds a short note to Claude's context, and Claude acts on it or writes you a highlighted tip: a large "
        "read, search or web result, a subagent run past the point where your own history says splitting pays, a plan approved "
        "on top of a lot of planning context, a message sent after a break that outlasted the prompt cache, "
        "or asking how background work is going while it still runs. It also flags how "
        "you prompt: a big task without a plan, small requests sent one at a time, or a huge paste. Vague "
        "corrections, the same request again and stopping Claude again and again are counted after the fact "
        "on Work habits, with no live note.",
        why="Advice at the moment it applies, and Claude can often act on it itself. Each note is about 50 to "
        "140 tokens, re-read on every later reply of the session. Claude Code waits for the hook after each "
        "read, search or web result and each message you send. A hook after each reply runs in the background and keeps "
        "only the time and size of Claude's newest reply, so the cache check is right after a resume.",
        powers=("context", "tool_output", "delegation", "planning"),
        hooks=("UserPromptSubmit", "PostToolUse", "Stop"),
    ),
    Metric(
        id="brief_templates",
        group="coaching",
        section="coaching",
        title="Brief templates",
        what="Checklists per kind of task, built from what your own requests tend to lack, on "
        "Work habits to copy. Turned on, it also adds a /cg-brief skill you run with a request: Claude "
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
        what="A /cg-feedback skill you run after a piece of work. It asks four checkbox questions: the "
        "outcome, what slowed it, whether it was worth the tokens, and what would have helped. After an "
        "approved plan it asks a fifth: whether the build could have started fresh from the plan.",
        why="Cost per piece of work that met its goal, which outranks what Claude reports about itself. "
        "The plan answer tells the fresh-session tip and the suggested profile how you work.",
        powers=("outcome", "planning", "profiles"),
        tag="[cg-fb: outcome=… slow=… worth=… helped=… handoff=…]",
    ),
    Metric(
        id="feedback_note",
        group="feedback",
        section="feedback",
        title="Feedback reminder in the status line",
        what="A second status line reminding you to run /cg-feedback, and the same line on the dashboard "
        "banner.",
        why="A reminder that costs nothing: the status line is never sent to Claude.",
        powers=("outcome",),
    ),
    Metric(
        id="feedback_reminder",
        group="feedback",
        section="feedback",
        title="Feedback reminder from Claude",
        what="Claude adds a highlighted note suggesting /cg-feedback once a session, when it finishes its first "
        "piece of work.",
        why="For people without the status line, such as in the desktop app. Costs a few output tokens once a "
        "session.",
        powers=("outcome",),
        hooks=("SessionStart",),
        main_extra="The first time in this session you finish a piece of work the user asked for, add this before "
        f"your tag, after a blank line, and never again after that:\n{REMINDER_LABEL} {FEEDBACK_REMINDER_LINE}",
        extra_before_tag=True,
        main_extra_untagged="The first time in this session you finish a piece of work the user asked for, end your "
        f"reply with this, after a blank line, and never again after that:\n{REMINDER_LABEL} {FEEDBACK_REMINDER_LINE}",
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
#: (``config.set_capture``): the /cg-feedback survey, its reminder note,
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
RETIRED_METRIC_IDS: tuple[str, ...] = ("detour", "web", "spawn", "rules")

#: The persistent feedback note (``feedback_note``): the status line's
#: second line and the dashboard banner show it word for word.
FEEDBACK_NOTE = "Finished a piece of work? Run /cg-feedback: a few ticks make your savings tips fit how you work."


# -- the /cg-feedback questions --------------------------------------------

#: The feedback skill: the user runs it as ``/cg-feedback``, from
#: ``~/.claude/skills/cg-feedback/SKILL.md``.
FEEDBACK_SKILL = "cg-feedback"

#: ``[cg-fb: ...]``: the tag the skill ends with, carrying the answers.
FEEDBACK_TAG = "cg-fb"
#: The tag's name until 0.12.1.
OLD_FEEDBACK_TAG = "tl-fb"


@dataclass(frozen=True, slots=True)
class FeedbackQuestion:
    """One /cg-feedback question, asked with AskUserQuestion and offered
    as checkboxes on the dashboard's Sessions tab."""

    #: The ``[cg-fb: ...]`` key its answer is written under.
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

#: Every question a ``[cg-fb: ...]`` tag or an answer may carry.
ALL_FEEDBACK_QUESTIONS: tuple[FeedbackQuestion, ...] = FEEDBACK_QUESTIONS + (HANDOFF_QUESTION,)

#: ``[cg-fb: ...]`` key -> the words its answer may take.
FEEDBACK_VOCAB: dict[str, tuple[str, ...]] = {q.key: tuple(o[0] for o in q.options) for q in ALL_FEEDBACK_QUESTIONS}

#: ``[cg-fb: ...]`` keys whose value is a comma list of words.
FEEDBACK_LIST_KEYS = frozenset(q.key for q in ALL_FEEDBACK_QUESTIONS if q.multi)

#: The words a dashboard rating (the Sessions tab's checkboxes) may
#: take: the four questions every run asks.
RATING_VOCAB: dict[str, tuple[str, ...]] = {q.key: FEEDBACK_VOCAB[q.key] for q in FEEDBACK_QUESTIONS}


def feedback_skill_text() -> str:
    """``SKILL.md`` for ``/cg-feedback``. ``disable-model-invocation``
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


# -- the /cg-brief checklists -----------------------------------------------

#: The brief skill: the user runs it as ``/cg-brief <request>``, from
#: ``~/.claude/skills/cg-brief/SKILL.md``.
BRIEF_SKILL = "cg-brief"

#: The names the two skills had before -> their names now: ``tl-`` (for
#: claude-token-lens, this tool's name until 0.9.0) until 0.12.0, and
#: ``cl-`` in 0.12.0 by mistake. A copy of ours still under an old name is renamed by
#: ``update --finish`` or ``capture <switch> on``.
RENAMED_SKILLS = {
    "tl-feedback": FEEDBACK_SKILL,
    "cl-feedback": FEEDBACK_SKILL,
    "tl-brief": BRIEF_SKILL,
    "cl-brief": BRIEF_SKILL,
}

#: ``/cg-feedback`` under any of its names: transcripts from before
#: 0.12.1 ran it as ``/tl-feedback`` or ``/cl-feedback``.
FEEDBACK_SKILL_NAMES = frozenset({FEEDBACK_SKILL, *(o for o, n in RENAMED_SKILLS.items() if n == FEEDBACK_SKILL)})

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
    """``SKILL.md`` for ``/cg-brief``: check a request against its kind of
    task's checklist and ask once for what is missing, or start. Like
    ``/cg-feedback`` it is user-invoked only and names no model."""
    lines = [
        "---",
        f"name: {BRIEF_SKILL}",
        "description: Check a request against a short checklist for its kind of task before starting, for "
        "ClaudeGlass.",
        "disable-model-invocation: true",
        "---",
        "",
        "The user wants their request checked before the work starts, for ClaudeGlass, so less is spent "
        "finding things out. The request is the text after /cg-brief; when there is none, it is the user's "
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
NOTE_INTRO = (
    "The user turned on ClaudeGlass metrics capture, to see where their tokens go. "
    "If you are a subagent, ignore this note."
)
MAIN_TAG_INTRO = (
    "End your final reply to each user message with one line, [cg: key=word ...], using only these keys and words:"
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
    the tags (``tagger``), the main note asks for no ``[cg: ...]`` tag.

    ``hooks/capture_hook.py`` builds the same text from
    ``capture-catalogue.json`` (:func:`export_json`); a test holds the two
    to the same output.
    """
    if scope == "subagent":
        # A subagent is asked for nothing: Haiku judges its run afterwards
        # (agent_judge_text), so its report is exactly what it would be.
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
    """The ``[cg: ...]`` keys the metrics in ``ids`` ask for, in
    catalogue order."""
    wanted = set(ids)
    return tuple(m.id for m in METRICS if m.id in wanted and m.main_line)


#: Key lines Haiku gets in place of the note's, spelling out each word:
#: it sees an excerpt, not the work, and ``scripts/eval-tagger.py`` found
#: these keys read differently without them (running the tests read as
#: ``check=run``, a README typo fix as ``task=bugfix``). The rest are
#: the note's own lines.
JUDGE_LINES = {
    "task": f"task: {'|'.join(TAG_VOCAB['task'])} (the kind of work asked for: docs = documentation or comments "
    "only; ops = CI, build, deploy or configuration; test = tests only; research = finding something out; "
    "review = judging existing work; debug = finding a fault's cause; chat = no work asked for)",
    "brief": "brief: clear|partial|vague (how complete the request was: clear = what to change and what done looks "
    "like; partial = the goal without the details; vague = neither)",
    "shift": "shift: new|build|grew|redo|fix, only after an earlier message (new = an unrelated task; build = a "
    "next step on top of the last task; grew = more asked of the same task; redo = the same task done another "
    "way; fix = fixing a fault in the last work)",
    "size": "size: xs|s|m|l|xl (how big the work was: xs = a line or two, or only an answer; s = a small change; "
    "m = a feature with its tests; l = many files; xl = a large change)",
    "plan": "plan: none|made|following|deviated (none = no plan; made = Claude wrote one this turn, as a plan "
    "file or as steps in its reply; following = Claude carried out one written earlier; deviated = Claude "
    "departed from one written earlier)",
    "prior": "prior: needed|some|none (how much the work relied on the earlier conversation: needed = the "
    "message only makes sense with it; some = it helped; none = a fresh request)",
    "check": "check: targeted|full|build|run|manual|none (how Claude verified its change: targeted = ran the "
    "tests for the part changed; full = ran the whole test suite; build = only built or type-checked; run = "
    "ran the program itself to see it work; manual = only read it back; none = no check, or nothing changed)",
}


def judge_text(ids) -> str:
    """What Haiku is told when it writes the main session's tags
    (``tagger = "haiku"``): a line for each key the note would ask
    Claude for (:data:`JUDGE_LINES`, else the note's own); ``""`` when
    none of ``ids`` asks for a key. ``hooks/capture_hook.py`` builds the
    same text (``build_judge_prompt``)."""
    keys = set(tagged_keys(ids))
    if not keys:
        return ""
    lines = [JUDGE_LINES.get(m.id, m.main_line) for m in METRICS if m.id in keys]
    return "\n".join([JUDGE_INTRO, *lines, JUDGE_RULE])


def agent_metric_ids(ids) -> tuple[str, ...]:
    """The agent metrics among ``ids`` (:data:`AGENT_JUDGE_KEYS`), in
    catalogue order."""
    wanted = set(ids)
    return tuple(m.id for m in METRICS if m.id in wanted and m.id in AGENT_JUDGE_KEYS)


def agent_judge_text(ids) -> str:
    """What Haiku is told when it judges a finished agent run: a line for
    each agent metric in ``ids``; ``""`` when there's none.
    ``hooks/capture_hook.py`` builds the same text
    (``build_agent_judge_prompt``)."""
    lines = [METRICS_BY_ID[metric_id].sub_line for metric_id in agent_metric_ids(ids)]
    if not lines:
        return ""
    return "\n".join([AGENT_JUDGE_INTRO, *lines, AGENT_JUDGE_RULE])


def tool_note_text(metric_id: str) -> str:
    """The note a PostToolUse hook adds after a large result
    (``big_output``) or a web result (``web``)."""
    metric = METRICS_BY_ID.get(metric_id)
    if metric is None or not metric.tool_note:
        return ""
    return f"{NOTE_MARKER}{NOTE_VERSION} {metric.id}\n{metric.tool_note}"


#: The tool names each PostToolUse-triggered metric matches (see
#: ``hook_specs``'s own matchers).
_POST_TOOL_USE_TOOLS = {"big_output": BIG_OUTPUT_TOOLS}


def tool_suffix_chars(metric_id: str) -> int:
    """CAP-10: Claude Code's own PostToolUse wrap names the specific
    tool that matched, not the whole matcher pattern (its own debug log
    shows ``"PostToolUse:Write"``, not ``"PostToolUse:Read|Grep|..."``)
    -- estimated here, before any real note has been measured, as the
    average length of ``metric_id``'s own matcher's tool names, plus the
    ``:`` that joins it to the event name."""
    names = _POST_TOOL_USE_TOOLS.get(metric_id, ())
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
    for a background hook, which would drop the turn. Coaching notes also
    add a background ``Stop`` entry (unless one of those already runs it),
    which prints nothing: it only keeps the newest reply's time and size
    for the cold-return receipt."""
    wanted = set(ids)
    haiku = HAIKU_TAGGER_HOOK in wanted and bool(tagged_keys(wanted))
    main = any(m.id in wanted and ((m.main_line and not haiku) or m.main_extra) for m in METRICS)
    agents = bool(agent_metric_ids(wanted))
    coach = "coaching_notes" in wanted
    specs: list[tuple[str, str, str, bool]] = []
    if main:
        specs.append((HOOK_SCRIPT, "SessionStart", SESSION_START_MATCHER, False))
    if agents:
        # In the foreground, as Stop is for Haiku: it only hands the run to
        # a worker and returns, and claude -p exits without waiting for a
        # background hook.
        specs.append((HOOK_SCRIPT, "SubagentStop", "", False))
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
    if coach and not any(spec[1] == "Stop" for spec in specs):
        # Writes the newest reply's time and size to coach-state.json and
        # prints nothing, so nothing waits on it.
        specs.append((HOOK_SCRIPT, "Stop", "", True))
    return tuple(specs)


def export_json() -> dict:
    """What ``hooks/capture_hook.py`` needs from this module, as JSON-safe
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
        "result_tools": list(COACHING_TOOLS),
        "result_image_max_tokens": RESULT_IMAGE_MAX_TOKENS,
        "result_image_patch_px": RESULT_IMAGE_PATCH_PX,
        "result_persist_chars": dict(RESULT_PERSIST_CHARS),
        "result_preview_chars": RESULT_PREVIEW_CHARS,
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
            "lines": dict(JUDGE_LINES),
            "vocab": {key: list(words) for key, words in TAG_VOCAB.items()},
            "list_keys": sorted(LIST_KEYS),
            "agent": {
                "intro": AGENT_JUDGE_INTRO,
                "rule": AGENT_JUDGE_RULE,
                "limits": dict(AGENT_JUDGE_LIMITS),
                "keys": {metric_id: list(keys) for metric_id, keys in AGENT_JUDGE_KEYS.items()},
                "vocab": {key: list(words) for key, words in AGENT_JUDGE_VOCAB.items()},
            },
        },
        "coaching": {
            "marker": COACH_MARKER,
            "version": COACH_VERSION,
            "file": COACHING_FILE,
            "state_file": COACH_STATE_FILE,
            "thresholds": dict(COACHING_THRESHOLDS),
            "text": dict(COACHING_TEXT),
            "quiet_how": dict(COACHING_QUIET_HOW),
            "notice": dict(COACHING_NOTICE),
            "interrupt_prefix": INTERRUPT_PREFIX,
            "not_typed_prefixes": list(NOT_TYPED_PREFIXES),
            "not_typed_turn_origins": list(NOT_TYPED_TURN_ORIGINS),
            "adjust_pattern": ADJUST_PATTERN,
            "remind_pattern": REMIND_PATTERN,
            "go_pattern": GO_PATTERN,
            "go_max_chars": GO_MAX_CHARS,
            "status_pattern": STATUS_PATTERN,
            "status_max_chars": STATUS_MAX_CHARS,
            "background_launch_pattern": BACKGROUND_LAUNCH_PATTERN,
            "background_scan_chars": BACKGROUND_SCAN_CHARS,
            "task_notification_prefix": TASK_NOTIFICATION_PREFIX,
            "reply_scan_chars": REPLY_SCAN_CHARS,
            "reply_fence_pattern": REPLY_FENCE_PATTERN,
            "reply_tip_block_pattern": REPLY_TIP_BLOCK_PATTERN,
            "reply_inline_code_pattern": REPLY_INLINE_CODE_PATTERN,
            "reply_url_pattern": REPLY_URL_PATTERN,
            "reply_quoted_pattern": REPLY_QUOTED_PATTERN,
            "reply_reminder_line_pattern": REPLY_REMINDER_LINE_PATTERN,
            "reply_tags_pattern": REPLY_TAGS_PATTERN,
            "reply_list_start_pattern": REPLY_LIST_START_PATTERN,
            "reply_unit_pattern": REPLY_UNIT_PATTERN,
            "reply_question_trim": REPLY_QUESTION_TRIM,
            "test_command_split_pattern": TEST_COMMAND_SPLIT_PATTERN,
            "test_heredoc_pattern": TEST_HEREDOC_PATTERN,
            "test_prefix_pattern": TEST_PREFIX_PATTERN,
            "test_program_pattern": TEST_PROGRAM_PATTERN,
            "test_runner_pattern": TEST_RUNNER_PATTERN,
            "test_no_run_pattern": TEST_NO_RUN_PATTERN,
            "test_target_pattern": TEST_TARGET_PATTERN,
            "test_bare_target_pattern": TEST_BARE_TARGET_PATTERN,
            "test_bare_target_runner_pattern": TEST_BARE_TARGET_RUNNER_PATTERN,
            "test_no_target_pattern": TEST_NO_TARGET_PATTERN,
            "test_whole_suite_pattern": TEST_WHOLE_SUITE_PATTERN,
            "edit_tools": list(EDIT_TOOLS),
            "how": {key: dict(variants) for key, variants in COACHING_HOW.items()},
            "asks_pattern": ASKS_PATTERN,
            "config_path_pattern": CONFIG_PATH_PATTERN,
            "shell_write_pattern": SHELL_WRITE_PATTERN,
            "review_pattern": REVIEW_PATTERN,
            "prose_noise_pattern": PROSE_NOISE_PATTERN,
            "plan_heading_pattern": PLAN_HEADING_PATTERN,
            "plan_numbered_pattern": PLAN_NUMBERED_PATTERN,
            "plan_doc_chars": PLAN_DOC_CHARS,
            "plan_doc_items": PLAN_DOC_ITEMS,
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

#: Characters the "[cg: " and "]" around a reply tag add.
_TAG_FRAME_CHARS = 6


def asks_claude(metric_id: str) -> bool:
    """Whether a metric has Claude read or write something, and so uses
    tokens."""
    m = METRICS_BY_ID.get(metric_id)
    return bool(m and (m.main_line or m.sub_line or m.main_extra or m.sub_extra or m.tool_note))


def rough_tokens(ids, tagger: str = DEFAULT_TAGGER) -> dict[str, int]:
    """Rough sizes in tokens (characters / 4) for the metrics in ``ids``:
    the note at each session start, clear or compaction
    (``session_note``); at each subagent start (``subagent_note``) and
    per subagent report (``report_tag``), both now always 0, as an agent
    is asked for nothing; the tag Claude writes per reply (``reply_tag``),
    none while Claude Haiku writes the tags (``tagger``); the /cg-feedback
    reminder Claude adds once a session (``reminder``); the note after a
    large or web tool result (``tool_note``). Amounts measured from
    transcripts replace these once capture has run."""
    enabled = [METRICS_BY_ID[i] for i in ids if i in METRICS_BY_ID]
    main, sub = note_text(ids, "main", tagger=tagger), note_text(ids, "subagent")
    # The /cg-feedback reminder comes once a session, not with every reply.
    reminder = sum(m.out_chars for m in enabled if m.main_extra and m.group == "feedback")
    if tagger == "haiku" and any(m.main_line for m in enabled):
        reply = frame = 0  # no tag at all
    else:
        reply = sum(m.out_chars for m in enabled if m.main_line or (m.main_extra and m.group != "feedback"))
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
        "reminder": round(reminder / 4),
        # Not tokens of Claude's: 1 when each agent run gets a Haiku call.
        "agent_judge": 1 if agent_metric_ids(ids) else 0,
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
    """The full ``[cg-fb: ...]`` tag with every question's whole
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
    if metric.id in AGENT_JUDGE_KEYS:
        return (
            "No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see "
            f"[Agent runs](#agent-runs)): \"{metric.sub_line}\""
        )
    if metric.id == "coaching_notes":
        return (
            "No tag. A hook adds a note only when a hint applies, and Claude acts on it or tells you in a "
            "highlighted tip: the note's first sentence says to write it and its last line is the tip, word for "
            "word, so every app shows it. "
            "Each hint and when it applies: [coaching.md](coaching.md)."
        )
    if metric.hooks:
        return "No tag. A hook records it directly; Claude is never asked."
    if metric.id == "brief_templates":
        return (
            "No tag. The checklists are on Work habits, and /cg-brief runs only when you type it; like any "
            "skill, its name and description are listed to Claude at each session start."
        )
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
        "Metrics capture is **opt-in**. Off by default, and off costs nothing: no tag is asked for, Claude "
        "Haiku is never asked, and no token is spent on it. Live coaching and your feedback have their own "
        "switches, and keep working while capture is off."
    )
    p("")
    p(
        f"Turned on, a hook (`{HOOK_SCRIPT}`) adds a short note to each session start, and asks Claude to end "
        "its replies with one line such as `[cg: task=bugfix brief=partial level=normal]` (or Claude Haiku "
        "writes it, see [Who writes the tags](#who-writes-the-tags)). A subagent is asked for nothing: its brief "
        "and its report are exactly what they would be, and Claude Haiku judges the run once it's done (see "
        "[Agent runs](#agent-runs)). The tag always sits at the end of the reply you already read — nothing is "
        "hidden — and nothing free-text is ever asked for: every word comes from a closed vocabulary (see "
        "[Privacy](#privacy) below)."
    )
    p("")
    p(
        "It costs tokens. The note is written to the prompt cache once, then read from it on every later "
        "reply of that session; the tag itself is a handful of output tokens on every reply, and each agent "
        f"run judged is a Haiku call of about ${JUDGE_USD_PER_CALL:.3f}. [Levels](#levels) below gives rough "
        "sizes; once capture is on, Setup › Capture "
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
        "session already carries the note from its start, so it is not asked again). Agent runs get no note, "
        "and nor does a subagent's own compaction, which the hook tells from the main session's by a subagent "
        "transcript that has just recorded one; the note also tells a subagent to ignore it. "
        "A level with agent metrics adds a Haiku call per agent run instead, however deep the agent is nested."
    )
    p("")
    p("| Level | What it adds | Note at session start | Haiku per agent run |")
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
        sub_cell = f"~${JUDGE_USD_PER_CALL:.3f}" if agent_metric_ids(ids) else "–"
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
        f"Every note (`{HOOK_MODULE}` builds the same text from `{CATALOGUE_FILE}`) opens with the same two "
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
        "A subagent gets no note, and a brief carries no marker: see [Agent runs](#agent-runs). Transcripts "
        "from before ClaudeGlass 0.11.0 may hold a subagent's own `[result: ...]` tag or a `[retry: ...]` brief "
        "marker; both are still read."
    )
    p("")
    p(f"The `/cg-feedback` skill ends with its own line: `{_feedback_tag_words()}`.")
    p("")
    p("If Claude writes more than one tag, the last one wins, key by key.")
    p("")

    # -- Who writes the tags ---------------------------------------------------
    p("## Who writes the tags")
    p("")
    standard = level_includes("standard")
    p(
        "By default Claude writes the `[cg: ...]` tag itself, at the end of its final reply to each of your "
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
        f"(up to {JUDGE_LIMITS['prompt']:,} characters), your message before it and the end of Claude's reply "
        "to that, how many you sent before, what Claude did (model calls, output tokens, tools used, the files "
        f"it changed, the first line of up to {JUDGE_LIMITS['commands']} shell commands, whether they ran tests, "
        "skills, subagents, tool errors, any plan-mode plan) and the end of its final reply (up to "
        f"{JUDGE_LIMITS['reply']:,} characters). Tool output is never in it."
    )
    p(
        f"- The worker runs `claude -p --model {JUDGE_MODEL}` with no tools, settings, MCP servers or saved "
        "session, through your own Claude Code login, with the excerpt on stdin, and no thinking. Haiku gets a "
        "line for each key Claude's note would have asked for, with each word spelled out, after "
        f"\"{JUDGE_INTRO}\" and before \"{JUDGE_RULE}\" It loads no "
        "settings file, so your hooks don't run inside it; a login that needs an `apiKeyHelper` from "
        "settings.json fails there, and `capture status` says so."
    )
    p(
        f"- Only the tag's words are kept, checked against the same vocabularies, in `<config-dir>/{JUDGE_DIR}/"
        "YYYY-MM.jsonl`, with the reply's id and what the call cost. What the transcript settles overrides "
        "Haiku: a plan-mode plan written that turn is `plan=made`; no skill run is never `skill=helped`; no "
        "file changed is `check=none`, and a test run is `check=targeted` or `full` by whether it picked tests; "
        "only documentation changed is `task=docs`; and a first message has no `shift` but `new`. A turn that "
        "got no tag says why: "
        + ", ".join(f"`{e}`" for e in JUDGE_ERRORS)
        + ". `capture status` counts both."
    )
    p(
        f"- Each call costs about ${JUDGE_USD_PER_CALL:.4f} (about 1,700 tokens read, 35 written), counted as "
        "capture's cost. On a subscription it counts toward your usage like any other Haiku use."
    )
    p(
        "- How well it works is measured by `scripts/eval-tagger.py`: recorded sessions with known right "
        "answers, judged by Haiku with and without thinking and by Sonnet, against Claude's own tags. "
        "[tagger-eval.md](tagger-eval.md) has the results."
    )
    p(
        "- The `Stop` entry runs in the foreground, since `claude -p` exits without waiting for a background "
        "hook, but only for as long as it takes to read the transcript's end. Deep's note after a large result "
        "isn't added, as no reply carries a tag for its word. Agent runs are Haiku's to judge whichever writes "
        "the main session's tags."
    )
    p("")

    # -- Agent runs ---------------------------------------------------------
    p("## Agent runs")
    p("")
    p(
        "A subagent is never asked for a tag, and a brief never carries a marker. Asked to end its report with "
        "`[result: ...]`, a subagent added it after an answer that had to be JSON only, breaking it, and the "
        "session that started it took the line for an injected instruction. So the agent metrics ("
        + ", ".join(f"`{metric_id}`" for metric_id in AGENT_JUDGE_KEYS)
        + ") are judged afterwards instead:"
    )
    p("")
    p(
        "- When a subagent finishes, the hook's `SubagentStop` entry reads its transcript and hands an excerpt "
        f"to the same worker as above: its type, its brief (up to {AGENT_JUDGE_LIMITS['brief']:,} characters, "
        "without the line a workflow script's harness puts before a brief it computed), "
        "what it did (model calls, tools used, the files it changed, the first line of up to "
        f"{AGENT_JUDGE_LIMITS['commands']} shell commands, tool errors), the end of its report (up to "
        f"{AGENT_JUDGE_LIMITS['report']:,} characters), or the start of the answer it handed back as structured "
        "output when that came last, as a workflow agent's does, and up to "
        f"{AGENT_JUDGE_LIMITS['earlier']} earlier agent "
        "runs of the session (their type and the start of their brief and the end of their report), so Haiku "
        "can tell a re-run. Tool output is never in it."
    )
    p(
        f"- Haiku is told \"{AGENT_JUDGE_INTRO}\", a line for each agent metric that is on, and \"{AGENT_JUDGE_RULE}\""
    )
    p(
        f"- Its words land in `<config-dir>/{JUDGE_DIR}/YYYY-MM.jsonl` beside the main session's, with the id of "
        "the agent's last reply, and are read as if the agent had written them. Each call costs about "
        f"${JUDGE_USD_PER_CALL:.3f}. Agents that set up Claude Code itself ("
        + ", ".join(f"`{t}`" for t in SKIP_AGENT_TYPES)
        + ") are skipped. `capture status` says how many runs were judged, what the calls cost and why any "
        "got no verdict (a `claude` command that isn't signed in, say: the desktop app keeps its own login), "
        "whoever writes the main session's tags."
    )
    p(
        "- Whether an agent used your CLAUDE.md rules (`rules`) can't be told from outside the agent, so it is "
        "no longer measured."
    )
    p("")

    # -- Privacy ------------------------------------------------------------
    p("## Privacy")
    p("")
    p(
        "Claude and Haiku write closed vocabularies only. Every `[cg: ...]` and `[cg-fb: ...]` word, and every "
        "`[result: ...]`, `[retry: ...]` and `[spawn: ...]` word in an older transcript, is checked against the "
        "lists on this page; anything else — "
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
        f"The hook (`{HOOK_SCRIPT}`, a small launcher), the module it runs (`{HOOK_MODULE}`) and its catalogue "
        f"(`{CATALOGUE_FILE}`) live side by side under `<config-dir>/hooks/`. A change that needs different hook entries (`capture on`, `level`, `enable`, "
        "`disable`, `tagger` or `connect`) also changes `~/.claude/settings.json`, and `capture remove` takes "
        "the entries out — each only after showing the diff and asking first, unless you pass `--yes`. "
        "`capture off` leaves the entries, which add nothing while it's off, and every other change writes "
        "only this tool's own `config.toml`."
    )
    p("")
    p(
        "- `claudeglass capture status` — the level, what's on, since when, and the cost measured so "
        "far. While big_output or web is on, it also prints Deep's actual measured wait (median and p90, "
        "over the last 7 days). It also flags any hook — ClaudeGlass's own or one of yours — that failed on "
        "most of its calls over the last 14 days, naming it (event name only, never a matcher or tool name), "
        "when it last failed, where to find it in `settings.json`, the trade-off, and the undo; this is only "
        "ever a printed prompt, never an automatic change. A hook that has stopped failing since is left out."
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
    p("- `claudeglass capture feedback on|off` — the `/cg-feedback` skill and its status-line reminder.")
    p("- `claudeglass capture brief on|off` — the `/cg-brief` skill.")
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

"""Work habits: how the way you work shapes what it costs, and the habits
worth trying (the ``habits`` report section and the Work habits tab).

Everything is worked out per *prompt cycle* (``capture.prompt_cycles``):
one message of yours and all the work that answered it, subagents at any
depth included. A workflow's agents belong to the message whose reply
started (or resumed) their run (``capture.WorkflowLaunches``), so their
cost, their overlap with what was already read and the message's redo cost
all count them. /cg-feedback runs are left out: they rate the work, they
aren't part of it.

A *piece of work* (``pieces.pieces_of``) is drawn from the transcript, so
``Habits.pieces`` has a row for every one, rated or not: a rated one has an
``outcome``. A message is *redone* when the cycles straight after it are
rework (``pieces``' rule, or your next message was a redo, a fix or a
correction): ``redo_cost`` is the whole run of them, counted once, and
the cycles in the run are not themselves redone. The rates per message
(``trend``) divide by the messages that asked for something
(``CycleFact.asks``), the ones you typed while Claude worked included.

Every playbook item says where its evidence came from, so you know how
far to trust it:

- ``reported``: what Claude (or Claude Haiku, judging an agent run) said
  about the work in a metrics-capture tag (``[cg: task=... brief=...
  level=...]``, ``[result: ...]``, ``[retry: ...]``). Claude judging its
  own work is low-trust. How well a model fits an agent's work is not
  asked of anyone: the agent tables count it from the runs
  (``AgentFact.probe_calls``, ``AgentFact.calls_before_edit``).
- ``inferred``: what the transcripts show without asking anyone: what
  your messages contained, tool output sizes, reads, retries, loops,
  context size, permission prompts.
- ``your feedback``: /cg-feedback answers and your ratings on the
  dashboard. They outrank the rest.

Savings are list-price USD over the report window, and the playbook
spreads them over the weeks it covers. Each is a rough figure with its
basis stated (the ``basis`` column): a habit never moves one number
cleanly. Carrying a tool result or an agent report is priced from the
reply after it to the next compaction, one cache write and then a cache
read per reply at each reply's own rates (fast mode, long context and
data residency included). That undercounts whenever the cache went cold,
so it is a floor.

Weeks start on Monday, and a day is midnight to midnight, in ``config.tz``
(the machine's own zone when it sets none): the same local days the
dashboard's windows count.

Only words from closed lists, counts and flags reach the tables; nothing
you or Claude wrote is kept.
"""

from __future__ import annotations

import bisect
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from . import capture as capture_mod
from . import capture_catalogue as catalogue
from . import classify
from . import events as events_mod
from . import known_savers
from . import model_gate
from . import pieces as pieces_mod
from . import quality
from .capture_tags import with_older_why
from .context_files import _parse_ts
from .discovery import local_day, to_local
from .handoff import MIN_FEEDBACK_ANSWERS, plan_carried, plan_shape, starting_context
from .model import PROMPT_FLAGS, Column, EventKind, Feedback, Recommendation, Section, Table, Turn
from .pricing import Pricing, effective_rates, price_turn
from .topology import agent_key

if TYPE_CHECKING:
    from .model import ReportModel

_READ_TOOLS = ("Read", "Grep", "Glob")
_SHELL_TOOLS = ("Bash", "PowerShell")
#: Agent reports are counted as reports (``short_reports``), not output.
_REPORT_TOOLS = ("Agent", "Task")
#: A tool call stopped before it ran, by denial bucket
#: (``events.denial_bucket_of``): auto mode blocked it or couldn't say,
#: or a deny rule or you turned it down. Not a plan you sent back, a
#: question you declined, a dialog you closed or a hook's block: those
#: are no call you refused.
_BLOCKED = ("auto_blocked", "auto_unavailable")
_REFUSED = ("refused",)
#: Tools whose permission prompt is a dialog Claude Code shows whatever
#: your rules say (a plan to approve, a question to answer), so no allow
#: rule would end it.
_DIALOG_TOOLS = ("ExitPlanMode", "AskUserQuestion")
_EXPLORE_AGENT = "Explore"
_HIGH_EFFORT = ("high", "xhigh", "max")
#: Playbook items built from the ``level`` Claude reported, whose
#: confidence is capped when self-reports don't carry signal (see
#: ``_self_report_calibration``).
_LEVEL_ITEMS = frozenset({"effort_fit", "skip_plan_easy", "plan_hard"})
#: Easy-at-high-effort messages before ``effort_fit`` fires -- the same
#: number as ``recommend._EFFORT_MIN_MESSAGES`` (UX-3, "one shared effort
#: threshold"): the habit item is ``COVERED_BY`` the ``effort-mismatch``
#: rule, which reads this same ``habits_effort_fit`` data, so the two
#: shouldn't disagree on when there's enough to say something.
_EFFORT_MIN_MESSAGES = 5

#: A message whose main session ran this many reads and searches itself
#: did research an Explore agent could have done.
RESEARCH_READS = 5
#: What an Explore agent's report hands back to the main session, about.
EXPLORE_REPORT_TOKENS = 1_500
#: An agent report over this many tokens is long.
LONG_REPORT_TOKENS = 2_000
#: What a report asked to be short comes back at, about.
SHORT_REPORT_TOKENS = 800
#: One reply's tool output this size or bigger is a big output (the
#: Deep level's own threshold).
BIG_OUTPUT_TOKENS = catalogue.BIG_OUTPUT_TOKENS
#: Earlier work still in context worth clearing, in tokens.
STALE_TOKENS = 20_000
#: A break this long lets the cache go cold (the 1-hour TTL at most).
LONG_BREAK_S = 3_600
#: Without a size tag, a message this many replies long was a large ask.
LARGE_TURNS = 60
#: A skill Claude reached for after this many replies came late.
LATE_SKILL_TURNS = 3
#: How many messages a comparison group needs before it's shown at all
#: (PROF-04: a setup only gets ticked as "cheaper" in the tasks goal
#: once it also clears TICK_MIN_GROUP -- see profiles.goals._tasks).
MIN_GROUP = 5
#: CAP-6: a message reported ``level=easy`` whose cost lands at or above
#: this percentile among same-``task`` messages is flagged as an effort
#: contradiction (:func:`contradiction_flags`'s ``easy_high_effort``).
EASY_HIGH_EFFORT_PCT = 0.75
#: How many messages a "cheaper" setup needs before the tasks goal
#: ticks it for you rather than just surfacing it (PROF-04).
TICK_MIN_GROUP = 20
#: An agent type's model-swap saving needs at least this share before
#: ``habits_agents_by_task`` names a cheaper model for it (mirrors
#: ``profiles.goals._models``' own floor).
CHEAPER_MODEL_MIN_PCT = 5.0
#: Model families, cheapest first, for naming a setup.
_FAMILIES = ("haiku", "sonnet", "opus", "fable")
#: How many weeks the trend covers, and the fewest messages that asked for
#: something a week needs to count towards it.
TREND_WEEKS = 8
TREND_MIN_CYCLES = 3
#: Corrections and adjustments after an approved plan that make it a plan
#: you had to fix (:func:`fixes_after_plan`), counting the ones you typed
#: while Claude worked as well as the ones that opened a message.
PLAN_FIXES_MIN = 3
#: Known weeks before a fall counts as a habit already picked up.
TREND_MIN_ADOPTED = 4
#: Weeks with enough messages to measure before a trend says anything but
#: "new".
TREND_MIN_WEEKS = 3
#: A habit that saves less than this much in list-price USD is too small to
#: tell you about (``effort_fit`` over a week, ``skip_plan_easy`` over the
#: window).
MIN_SAVING = 1.0
#: ``brief_clearly``: the chance that a partial or vague ask in one task
#: cost more than a clear one of the same task, below which the card stays
#: hidden. 0.5 is a coin toss.
BRIEF_MIN_PROBABILITY = 0.6
#: ``effort_fit``: points of thinking share easy work must have over hard
#: work at the same effort before high effort on easy work is the cause.
EFFORT_EASY_OVER_HARD_PTS = 10.0

#: Playbook item -> (theme, habit to try).
ITEMS: dict[str, tuple[str, str]] = {
    "split_large": ("breakdown", "Split large asks into planned steps"),
    "batch_small": ("breakdown", "Batch small asks into one session"),
    "clear_between": ("context", "Start a new session between unrelated tasks"),
    "brief_clearly": ("information", "Say what you want and what done looks like"),
    "name_files": ("information", "Name the files you already know"),
    "paste_errors": ("information", "Paste the failing output with a bug"),
    "explore_research": ("research", "Hand broad searches to an Explore agent"),
    "plan_hard": ("planning", "Plan hard work before building it"),
    "skip_plan_easy": ("planning", "Skip plan mode for easy changes"),
    "skill_early": ("skills", "Run the right skill at the start"),
    "skill_unneeded": ("skills", "Stop Claude loading skills that don't help"),
    "short_reports": ("delegation", "Ask agents for short reports"),
    "better_briefs": ("delegation", "Give agents a complete brief"),
    "flatten_nesting": ("delegation", "Pass what you know to agents instead of re-reading"),
    "quiet_output": ("tool_output", "Keep tool output small"),
    "targeted_checks": ("verification", "Check each change, and run the full suite once"),
    "allow_routine": ("waiting", "Allow the commands you always approve"),
    "state_limits": ("waiting", "Tell Claude up front what not to do"),
    "effort_fit": ("models", "Use lower effort for easy work"),
    "outcome_misses": ("outcome", "Look at what came before the misses"),
    "check_work": ("verification", "Have Claude check its work against what you asked"),
}

#: UX-3: habit item -> the ``recommend.py`` (or a module folded into it,
#: e.g. carry.py) rule id that prices the same underlying finding. When
#: that rule actually fires in a report, the habit's own saving would
#: double-count it -- ``apply_covered_by`` drops the habit's figure and
#: names the rule instead, once recommendations are known (the playbook
#: table is built before recommend() runs, so this can't happen inline
#: in ``playbook_table``). Every pair here is a genuine same-finding
#: match, not just a related theme -- e.g. ``better_briefs`` (give
#: agents a complete brief) is deliberately left unmapped to
#: ``spawn-task-prompt`` (keep the brief short): they pull in opposite
#: directions, not the same one twice.
COVERED_BY = {
    "effort_fit": "effort-mismatch",
    "short_reports": "agent-report-size",
    "quiet_output": "tool-output-carry",
}

#: An example of the habit, to copy or adapt.
EXAMPLES = {
    "split_large": "Plan this first and list the steps. Then do step 1 only and stop, so I can start a new "
    "session for step 2.",
    "batch_small": "While you're in there: also rename parse_row to read_row, and fix the heading in "
    "README.md.",
    "clear_between": "/clear, then the new task with the files it involves.",
    "brief_clearly": "Done when: the login test passes and nothing outside src/auth changes.",
    "name_files": "In src/auth/login.py, the handler that sets the session cookie ... (rather than 'the "
    "login code').",
    "paste_errors": "This fails: <paste the error and the command that shows it>. Fix it in src/...",
    "explore_research": "Use an Explore agent to find where retries are handled, and report the files and "
    "functions in under 200 words.",
    "plan_hard": "Before changing anything, plan this in plan mode and list the files you'll touch.",
    "skip_plan_easy": "Make this change directly, no plan needed: ...",
    "skill_early": "/<skill> <what you want>, as your first message for this kind of work.",
    "skill_unneeded": "Add disable-model-invocation: true to that skill's SKILL.md, so it loads only when you "
    "run it.",
    "short_reports": "... and report back in under 150 words: the answer and the files, nothing else.",
    "better_briefs": "Goal: ... Files: ... Done when: ... Report: under 150 words.",
    "flatten_nesting": "I've already read src/store.py: the part you need is save_rows(). Work from that; "
    "don't read it again.",
    "quiet_output": "Run the tests quietly and show only the failures, for example pytest -q 2>&1 | tail -30.",
    "targeted_checks": "Run only the tests for the files you changed; run the full suite once at the end.",
    "allow_routine": "/permissions, then allow the commands you approve every time, for example "
    "Bash(npm test:*).",
    "state_limits": "Don't run migrations or push; ask me first if you think you need to.",
    "effort_fit": "Lower the effort (/effort, or your effort level setting) for quick edits, and raise it for hard "
    "problems.",
    "outcome_misses": "Before you start, tell me your plan in three lines and what done will look like.",
    "check_work": catalogue.MISSED_IN_LINES[""],
}

#: ``explore_research``'s own title and example, when ``known_savers``
#: shows tokensave was at work in the window (``Habits.saver_active``):
#: its hook blocks every Explore agent call in a tokensave-indexed
#: project, so the generic advice above would just get redirected.
EXPLORE_RESEARCH_TOKENSAVE_TITLE = "Search with tokensave's tools instead of reading it yourself"
EXPLORE_RESEARCH_TOKENSAVE_EXAMPLE = (
    "tokensave_context \"where are retries handled\" for the concept, tokensave_search for a symbol by name, "
    "tokensave_files for files by path, then read only the lines you need."
)
#: Same switch for the playbook row's WHERE/trade-off (UX-8): ``UNDO``
#: doesn't name an Explore agent either way, so it needs no variant.
EXPLORE_RESEARCH_TOKENSAVE_WHERE = (
    "Nowhere in Claude Code's config. This is choosing to search with tokensave's own tools (tokensave_context, "
    "tokensave_search, tokensave_files) and read only the lines you need, instead of reading and searching "
    "yourself in the main session."
)
EXPLORE_RESEARCH_TOKENSAVE_TRADE_OFF = (
    "tokensave's tools return only what matched a query: a detail you'd have noticed reading the whole file "
    "yourself can get left out, the same trade-off an Explore agent's own summary has."
)

#: How each item's saving is worked out.
BASES = {
    "split_large": "half of what re-reading the growing context cost in those asks",
    "batch_small": "the start-up cache write each extra same-day session paid",
    "clear_between": "the earlier context each reply re-read; half of it after a long break",
    "brief_clearly": "half the gap to the median clear ask of the same kind and level, plan builds left out",
    "name_files": "half the gap in reads and searches to asks that named a file",
    "paste_errors": "half the gap to a bug report with the error in it",
    "explore_research": "carrying the reads and searches, less a short agent report",
    "plan_hard": "the redos of unplanned hard asks beyond the planned ones' rate",
    "skip_plan_easy": "what planning easy asks cost",
    "skill_early": "half of what was spent before Claude reached for the skill",
    "skill_unneeded": "not estimated",
    "short_reports": "carrying the part of each long report over a short one",
    "better_briefs": "what the agent runs restarted for the brief or the task cost",
    "flatten_nesting": "carrying the files agents read again",
    "quiet_output": "carrying big outputs; half of it unless Claude said none was needed",
    "targeted_checks": "half of what redoing or fixing unchecked changes cost",
    "allow_routine": "the replies after auto mode blocked a request",
    "state_limits": "the replies after a request was turned down",
    "effort_fit": "half the thinking on easy asks at high effort or above",
    "outcome_misses": "not estimated",
    "check_work": "half what the follow-ups cost that fixed something Claude missed",
}

#: UX-8: where each habit is put into practice -- a Claude Code setting
#: or file when there is one, otherwise a plain statement that it's about
#: how you write messages, not a setting.
WHERE = {
    "split_large": "Nowhere in Claude Code's config. This is how you phrase your own messages.",
    "batch_small": "Nowhere in Claude Code's config. This is when you start a new session.",
    "clear_between": "Nowhere in Claude Code's config. This is /clear or starting a fresh session.",
    "brief_clearly": "Nowhere in Claude Code's config. This is how you phrase your first message for a task.",
    "name_files": "Nowhere in Claude Code's config. This is what you put in your message.",
    "paste_errors": "Nowhere in Claude Code's config. This is what you put in your message.",
    "explore_research": (
        "Nowhere in Claude Code's config. This is choosing to spawn an Explore agent instead of reading "
        "and searching yourself in the main session."
    ),
    "plan_hard": "Nowhere in Claude Code's config. This is asking for plan mode before a hard change.",
    "skip_plan_easy": "Nowhere in Claude Code's config. This is choosing not to ask for plan mode.",
    "skill_early": (
        "Nowhere in Claude Code's config. This is running /<skill> as your first message instead of "
        "partway through."
    ),
    "skill_unneeded": "The skill's own SKILL.md frontmatter (disable-model-invocation: true).",
    "short_reports": "The Agent prompt you write when you spawn it, or the agent's own frontmatter file if it has one.",
    "better_briefs": "The Agent prompt you write when you spawn it.",
    "flatten_nesting": "The Agent prompt you write when you spawn it.",
    "quiet_output": (
        "Nowhere in Claude Code's config directly. This is how you invoke tools (head/tail, a digest "
        "script). The output-length caps in settings.json's env block have their own card."
    ),
    "targeted_checks": "Nowhere in Claude Code's config. This is which tests you ask Claude to run, and when.",
    "allow_routine": "/permissions, in the allow list for this project or your user settings.",
    "state_limits": (
        "Nowhere in Claude Code's config. This is what you put in your message, or a standing rule in "
        "CLAUDE.md if it should apply every time."
    ),
    "effort_fit": (
        "The effort level in settings.json, or the effort field in an agent's own file. /effort raises it "
        "for a single task without changing the setting."
    ),
    "outcome_misses": "Nowhere in Claude Code's config. This is reviewing your own /cg-feedback answers and messages.",
    "check_work": (
        "Nowhere in Claude Code's config. This is a line in your message, or in CLAUDE.md if it should apply "
        "every time."
    ),
}

#: UX-8: the cost of trying each habit -- what you give up, or risk, by
#: adopting it. ``allow_routine``'s is a security trade-off, not just a
#: cost one (a broad allow rule runs without asking again, for better or
#: worse).
TRADE_OFFS = {
    "split_large": (
        "Planning steps first costs a message up front, and a step you thought was separate sometimes "
        "turns out to depend on the next one anyway."
    ),
    "batch_small": (
        "Batching small asks into one session means an unrelated one waits until you're ready to send it. "
        "The session's context also keeps growing across all of them."
    ),
    "clear_between": (
        "A new session loses the context of what you were doing, so anything from the last task you "
        "still needed has to be explained again."
    ),
    "brief_clearly": (
        "Spelling out \"done\" up front takes longer to write than a short ask. It can also lock in a "
        "definition of done you'd have refined after seeing Claude's first attempt."
    ),
    "name_files": (
        "Naming files means checking you've got the right ones first. Naming the wrong one sends Claude "
        "down the wrong path faster than letting it search would have."
    ),
    "paste_errors": "A full paste can be long and include noise (stack frames, unrelated warnings) that costs tokens to send.",
    "explore_research": (
        "An Explore agent's report only carries back what it chose to include. A detail you'd have "
        "noticed reading the files yourself can get left out."
    ),
    "plan_hard": (
        "Planning first costs a round trip before any code changes, and a plan can go stale if you change "
        "your mind partway through building it."
    ),
    "skip_plan_easy": (
        "Skipping the plan means an easy-looking change that turns out to have a wrinkle gets discovered "
        "mid-edit instead of up front."
    ),
    "skill_early": (
        "Running the skill first commits you to its checklist before you've seen whether the task "
        "actually needs all of it."
    ),
    "skill_unneeded": (
        "With automatic loading off, Claude won't reach for the skill on its own, even when it would have "
        "helped. You have to run it by name."
    ),
    "short_reports": (
        "A shorter report can leave out detail you'd have wanted, especially for a task whose outcome is "
        "hard to summarise briefly."
    ),
    "better_briefs": "A complete brief takes longer to write than a short one, and repeats things the agent might have found out cheaply on its own.",
    "flatten_nesting": (
        "Passing along what you already know assumes it's still accurate; a file you read a while ago may "
        "have changed since."
    ),
    "quiet_output": "Trimming output before it enters context can cut a detail (an error further up a long log, say) that turns out to matter.",
    "targeted_checks": (
        "Running only the targeted tests during the work can miss a change's effect on an unrelated part "
        "of the suite. Only the final full run catches it."
    ),
    "allow_routine": (
        "This is a security trade-off, not only a convenience one. Once a command pattern is allowed, "
        "Claude Code runs any future call that matches it without asking again. That includes one you "
        "would have wanted to review this time. Keep the pattern as narrow as the commands you meant."
    ),
    "state_limits": (
        "Claude then has to check a stated limit before every relevant action. That can make it ask for "
        "confirmation even when the limit wouldn't have been hit."
    ),
    "effort_fit": "Lower effort can miss things on a task that turns out to be harder than it looked.",
    "outcome_misses": "None: reviewing past work costs time but changes nothing on its own.",
    "check_work": (
        "Checking adds a step at the end of every piece of work, and a long checklist can slow small changes "
        "down. Claude can also tick a step off without really checking it."
    ),
}

#: UX-8: how to undo each habit, once tried.
UNDO = {
    "split_large": "Nothing to undo: go back to asking for the whole thing in one message.",
    "batch_small": "Nothing to undo: go back to starting a session per ask.",
    "clear_between": "Nothing to undo: go back to continuing the same session.",
    "brief_clearly": "Nothing to undo: go back to writing briefer asks.",
    "name_files": "Nothing to undo: go back to describing the code instead of naming files.",
    "paste_errors": "Nothing to undo: go back to describing the failure in words.",
    "explore_research": "Nothing to undo: go back to reading and searching directly in the main session.",
    "plan_hard": "Nothing to undo: go back to building directly.",
    "skip_plan_easy": "Nothing to undo: go back to planning every change.",
    "skill_early": "Nothing to undo: go back to reaching for the skill only once you notice you need it.",
    "skill_unneeded": (
        "Remove disable-model-invocation from the skill's frontmatter (Claude Code shows the change "
        "before saving it)."
    ),
    "short_reports": "Stop asking for a short report, or remove the added instruction from the agent file.",
    "better_briefs": "Nothing to undo: go back to writing shorter briefs.",
    "flatten_nesting": "Nothing to undo: go back to letting agents re-read files themselves.",
    "quiet_output": "Nothing to undo: go back to letting full output through.",
    "targeted_checks": "Nothing to undo: go back to running the full suite after every change.",
    "allow_routine": "/permissions, then remove the rule (Claude Code shows the current allow list there).",
    "state_limits": "Nothing to undo: go back to not stating the limit, or remove it from CLAUDE.md if you added it there.",
    "effort_fit": (
        "Set the effort level back to its previous value, or raise it for one task with /effort without "
        "touching the setting."
    ),
    "outcome_misses": "Nothing to undo.",
    "check_work": "Nothing to undo: stop adding the line to your messages, or remove it from CLAUDE.md.",
}

#: A ``missing`` word -> what to add to a brief, for the templates (the
#: /cg-brief skill holds the same lines).
MISSING_LINES = {k: v for k, v in catalogue.BRIEF_LINES.items() if k != "report"}
_REPORT_LINE = catalogue.BRIEF_LINES["report"]

#: The checklist a kind of task starts from before your own data adds to it.
DEFAULT_CHECKLISTS = catalogue.BRIEF_CHECKLISTS
_DEFAULT_TEMPLATE_TASKS = ("bugfix", "feature", "refactor", "research")

#: A feedback answer's word -> the label you ticked, per question. The
#: older questions come first so the current one wins where a key is
#: shared; ``slow`` is only in the older run's.
_ANSWER_LABELS = {
    q.key: {o[0]: o[1] for o in q.options}
    for q in (*catalogue.LEGACY_FEEDBACK_QUESTIONS, *catalogue.FEEDBACK_QUESTIONS)
}
#: What slowed the work, in the words of both the older question (``slow``)
#: and the current one (``why``, what your follow-ups were mostly). Their
#: words differ, except ``none``, which a piece never keeps.
_SLOWED_LABELS = {**_ANSWER_LABELS["slow"], **_ANSWER_LABELS["why"]}


# -- facts ---------------------------------------------------------------------


class _Rates:
    """Per-token USD rates for each reply, each model resolved once."""

    def __init__(self, pricing: Pricing | None):
        self.pricing = pricing
        self._resolved: dict = {}

    def _resolve(self, model: str):
        if model not in self._resolved:
            self._resolved[model] = self.pricing.resolve_model(model)
        return self._resolved[model]

    def cost(self, turn: Turn) -> float:
        if self.pricing is None:
            return 0.0
        return price_turn(turn, self._resolve(turn.model)).total

    def _rates(self, turn: Turn):
        return None if self.pricing is None else effective_rates(turn, self._resolve(turn.model))

    def read(self, turn: Turn) -> float:
        rates = self._rates(turn)
        return rates.cache_read / 1e6 if rates is not None else 0.0

    def write(self, turn: Turn) -> float:
        rates = self._rates(turn)
        if rates is None:
            return 0.0
        return (rates.cache_write_1h if turn.cc_1h > turn.cc_5m else rates.cache_write_5m) / 1e6

    def output(self, turn: Turn) -> float:
        rates = self._rates(turn)
        return rates.output / 1e6 if rates is not None else 0.0

    def read_write(self, turn: Turn) -> tuple[float, float]:
        """``(read(turn), write(turn))`` from one shared ``_rates(turn)``
        call -- perf (ROB-P3-adjacent, S5): ``_CarryCost.__init__`` used
        to call ``.read()`` and ``.write()`` in two separate passes, each
        independently re-resolving the same turn's effective rates."""
        rates = self._rates(turn)
        if rates is None:
            return 0.0, 0.0
        read = rates.cache_read / 1e6
        write = (rates.cache_write_1h if turn.cc_1h > turn.cc_5m else rates.cache_write_5m) / 1e6
        return read, write


def _compacted(turn: Turn) -> bool:
    return EventKind.COMPACT_BOUNDARY in turn.preceding_event_kinds


class _CarryCost:
    """What keeping tokens in one transcript's context costs from a reply
    on: a cache write by the next reply, then a cache read by each reply
    after it until a compaction drops them."""

    def __init__(self, turns: list[Turn], rates: _Rates):
        self.turns = turns
        self.costs = [rates.cost(t) for t in turns]
        # Perf: read_write() shares one _rates(t) call for both, instead
        # of .read()/.write() each re-resolving it in a separate pass.
        read_write = [rates.read_write(t) for t in turns]
        self.reads = [r for r, _w in read_write]
        self.writes = [w for _r, w in read_write]
        n = len(turns)
        self._read_on = [0.0] * (n + 1)
        for i in range(n - 1, -1, -1):
            carries = i + 1 < n and not _compacted(turns[i + 1])
            self._read_on[i] = self.reads[i] + (self._read_on[i + 1] if carries else 0.0)

    def cost(self, i: int, tokens: float) -> float:
        """Carrying ``tokens`` that came back to reply ``i`` (a tool
        result or an agent report)."""
        j = i + 1
        if tokens <= 0 or j >= len(self.turns) or _compacted(self.turns[j]):
            return 0.0
        later = self._read_on[j + 1] if j + 1 < len(self.turns) and not _compacted(self.turns[j + 1]) else 0.0
        return tokens * (self.writes[j] + later)


@dataclass(slots=True)
class CycleFact:
    """One message of yours and the work that answered it, as numbers."""

    session_id: str
    ts: datetime | None
    week: str
    cost: float
    turns: int
    #: The cycle's tag with what the transcript settles put right
    #: (``Cycle.settled``), which everything that counts tags reads.
    tag: object = None
    #: The tag as Claude wrote it (``Cycle.tag``), for the checks that
    #: compare what it said with what happened; ``tag`` stands in when a
    #: fact was built without it.
    written: object = None
    flags: tuple[str, ...] = ()
    paste: bool = False
    #: Tokens in context when the work began beyond what a fresh session
    #: starts with, and what re-reading them cost across the message.
    stale_tokens: int = 0
    stale_cost: float = 0.0
    #: What writing them to the cache again cost after a long break.
    stale_rewrite: float = 0.0
    gap_s: float | None = None
    effort: str | None = None
    model: str = ""
    #: The dominant ``Turn.speed`` across the cycle's own turns
    #: ("standard"/"fast"/``None`` when no turn carried one) -- PROF-04's
    #: setup comparison splits by this alongside model and effort.
    speed: str | None = None
    #: This message's own cost with subagent spend left out -- the
    #: main-only figure ``habits_by_task``/``habits_setups`` show
    #: alongside ``cost`` (which, like ``capture._cycle_cost``, always
    #: includes subagents at any depth). PROF-06/PROF-04.
    main_cost: float = 0.0
    #: Re-reading the context this message itself built up.
    growth_cost: float = 0.0
    reads: int = 0
    read_tokens: int = 0
    read_carry: float = 0.0
    explore_agents: int = 0
    planned: bool = False
    plan_cost: float = 0.0
    #: (tool, tokens, carry cost) per big output.
    big_outputs: list = field(default_factory=list)
    #: (skill, by_you, replies before it, USD before it).
    skill_calls: list = field(default_factory=list)
    compactions: int = 0
    #: Tool calls auto mode blocked, and those a deny rule or you turned
    #: down, with what the replies after them cost.
    blocked: int = 0
    blocked_cost: float = 0.0
    refused: int = 0
    refused_cost: float = 0.0
    #: The cycles after this work redid it (``shift=redo``), fixed a fault
    #: in it (``shift=fix``) or corrected it, and what the whole run of
    #: them cost. A cycle that is itself one of them is not ``redone``.
    redone: bool = False
    redo_cost: float = 0.0
    #: CAP-5: derived fallback for ``tag.check`` -- a test-runner command
    #: ran during this message (``Turn.tests_run``, or for an older digest
    #: ``classify._matches_test_tool`` on its first command, the same one
    #: ``classify_purpose`` uses), whether or not Claude also self-reported
    #: ``check=``.
    checked_by_tool: bool = False
    output_cost: float = 0.0
    thinking_cost: float = 0.0
    #: Your feedback on the work this message belongs to, and where it
    #: came from: "answers" for /cg-feedback's question answers, "tag"
    #: for its `[cg-fb: ...]` line (a genuine run only -- SEC-P1), or
    #: "rating" for the dashboard. Self-report calibration trusts only
    #: "answers" and "rating": a "tag" is Claude's own report of the
    #: outcome, not yours.
    outcome: str | None = None
    outcome_source: str | None = None
    #: What your feedback said about the piece this message belongs to,
    #: for the habits that read it (``None``/empty without feedback): the
    #: /cg-feedback worth and would-have-helped answers.
    worth: str | None = None
    helped: tuple[str, ...] = ()
    #: Your answer to the plan check on this message, a word of
    #: ``capture_catalogue.PLAN_CHECK_WORDS`` (``""`` for none).
    plan_check: str = ""
    #: The first message of a piece whose follow-ups you said were mostly
    #: things it left out: what its ``missing`` words count for twice.
    left_out: bool = False
    #: Every token the message's work used, subagents included.
    tokens: int = 0
    #: How many messages of yours it holds that asked for something: its
    #: own, unless it is a go-ahead, a status check, a thank-you or a reply
    #: to a plan, and those you typed while it ran (``pieces.asks``). The
    #: rates per message divide by it.
    asks: int = 1
    #: Your next message would have counted as a redo, fix or correction,
    #: but your feedback says it wasn't: a change of mind, or something you
    #: only thought of after the plan. ``redone`` stays off for it, so
    #: rework and waste figures leave it out.
    excused: bool = False
    #: The message is in a piece whose /cg-feedback answers already count
    #: for ``check_work`` (``why`` has ``missed``, or ``plan`` is ``covered``):
    #: its plan check answer isn't counted again.
    in_missed_piece: bool = False
    #: The message carried out a plan you approved: it approved one in the
    #: dialog and went on to build, came after one you approved by typing,
    #: or Claude said it followed or departed from one. What building a plan
    #: costs says little about how clear the ask was.
    executes_plan: bool = False
    #: Its opening message was a go-ahead, a thank-you or a status check.
    quiet: bool = False
    #: The wait before it was a usage-limit pause, not a break you took.
    limit_pause: bool = False
    #: The Capture banner leaves it out of its tagged share: it was sent
    #: before capture was turned on, in a session capture never touched, or
    #: it is one coverage can't fairly judge (cut off, or stopped at the
    #: token limit before Claude could tag it).
    outside_capture: bool = False


@dataclass(slots=True)
class AgentFact:
    """One subagent run, at any depth. A workflow's agents are runs too:
    ``level``, ``task`` and ``overlap_*`` come from the message that
    started their workflow."""

    session_id: str
    agent_type: str
    week: str
    cost: float
    depth: int = 1
    model: str = ""
    report_tokens: int = 0
    report_carry: float = 0.0
    capped: bool = False
    result: str | None = None
    rules: str | None = None
    brief: str | None = None
    missing: tuple[str, ...] = ()
    retry: str | None = None
    spawn: str | None = None
    #: The run's model calls (replies, one per message id), and how many of
    #: them were a single read-only probe (:func:`_is_probe`).
    calls: int = 0
    probe_calls: int = 0
    #: How many calls came before the first one that changed your files
    #: (:func:`_calls_before_edit`); ``None`` when the run changed none.
    calls_before_edit: int | None = None
    overlap_reads: int = 0
    overlap_cost: float = 0.0
    #: The ``level`` and ``task`` Claude gave the message the agent
    #: worked on.
    level: str | None = None
    task: str | None = None
    #: False for a workflow's agent: the main session didn't spawn it, and
    #: nothing it did is a habit of yours.
    direct: bool = True
    #: The context size at every reply of the run, added up: what the run
    #: read in all, before any cache discount.
    context_tokens: int = 0


@dataclass(slots=True)
class Piece:
    """A piece of work: the messages one /cg-feedback answer rates, or a
    session you rated on the dashboard, or, for work no answer covers, a
    piece the transcript shows (``source`` ``transcript``, with no
    ``outcome``)."""

    #: ``None`` for a piece nobody rated.
    outcome: str | None
    cost: float
    cycles: int
    task: str | None
    slow: tuple[str, ...]
    helped: tuple[str, ...]
    source: str
    #: ``handoff.plan_shape`` of the messages it covers.
    shape: str = "no_plan"
    worth: str | None = None
    #: The /cg-feedback handoff answer, asked after an approved plan.
    handoff: str | None = None
    #: What your follow-ups were mostly (the current question; an older
    #: run's ``slow`` answer is in ``slow``).
    why: tuple[str, ...] = ()
    #: Where the thing Claude missed was (``why`` has ``missed``).
    missed_in: str | None = None
    #: After an approved plan: whether it covered what you then fixed.
    plan: str | None = None
    #: Whether a tip ClaudeGlass showed was right, and which tip.
    tip: str | None = None
    tip_hint: str | None = None
    #: The follow-up messages in the piece (every message after the first
    #: that isn't a go-ahead or a status check, as the hook counts them),
    #: what their replies cost, and the tokens they used.
    followups: int = 0
    followup_cost: float = 0.0
    followup_tokens: int = 0
    #: The plan check words answered in the piece (``PlanCheck.word``).
    plan_checks: tuple[str, ...] = ()
    #: You answered what slowed the work or what your follow-ups were, even
    #: if the answer was nothing.
    why_given: bool = False
    #: You answered what would have made it cheaper, even if the answer
    #: was nothing.
    helped_given: bool = False
    #: A dashboard rating that is the only answer about its session's work
    #: (no /cg-feedback run rated any of it): it counts as a /cg-feedback
    #: piece does (:func:`_answered`).
    alone: bool = False
    #: Of a piece drawn from the transcript: the messages that asked for
    #: something, those that were rework after it was delivered, and what
    #: the rework cost (``pieces.WorkPiece``).
    substantive: int = 0
    rework: int = 0
    rework_cost: float = 0.0

    def reasons(self) -> set[str]:
        """What your follow-ups were mostly, in ``why``'s words: this
        piece's own answer, or an older run's ``slow`` answer read as one
        (``capture_catalogue.SLOW_TO_WHY``)."""
        older = {catalogue.SLOW_TO_WHY[w] for w in self.slow if w in catalogue.SLOW_TO_WHY}
        return {*self.why, *older} - {"none"}


@dataclass(slots=True)
class PlanFix:
    """The first plan approved in a piece of work, and the fixes after it."""

    #: ``handoff.plan_shape`` of the session.
    shape: str
    #: Corrections and adjustments you typed after the approval, and those
    #: you typed while Claude worked. A queued message is counted once per
    #: reply it arrived with, however many were queued.
    typed: int = 0
    queued: int = 0
    #: What the messages that held a fix cost.
    cost: float = 0.0

    @property
    def fixes(self) -> int:
        return self.typed + self.queued


@dataclass(frozen=True, slots=True)
class PlanFixes:
    """Fixes after a plan, over the plans :func:`fixes_after_plan` counted."""

    plans: int = 0
    #: Plans with :data:`PLAN_FIXES_MIN` fixes or more, and the fixes in all
    #: of them, those you typed while Claude worked among them.
    fixed: int = 0
    fixes: int = 0
    queued: int = 0
    #: What the messages that held a fix cost, in the plans fixed that often.
    cost: float = 0.0


@dataclass(slots=True)
class SessionShape:
    """Whether you planned and built in one main session."""

    #: ``handoff.plan_shape``: "plan_build", "plan_only" or "no_plan".
    shape: str
    cost: float
    #: ``handoff.plan_carried``: tokens, or ``None`` without a plan.
    carried: int | None


@dataclass(slots=True)
class Habits:
    """Everything the tables and the playbook are built from."""

    cycles: list[CycleFact] = field(default_factory=list)
    agents: list[AgentFact] = field(default_factory=list)
    pieces: list[Piece] = field(default_factory=list)
    #: Every piece of work drawn from the transcripts, rated or not
    #: (``pieces.WorkPiece``): what :mod:`rework` is built from.
    work_pieces: list = field(default_factory=list)
    #: One per piece of work with an approved plan (:class:`PlanFix`).
    plan_fixes: list[PlanFix] = field(default_factory=list)
    #: One per main session with a message of yours.
    shapes: list[SessionShape] = field(default_factory=list)
    #: (session_id, day, project, start-up premium USD) of sessions that
    #: were one small ask; ``reported`` when a size tag said so.
    small_sessions: list = field(default_factory=list)
    #: Permission prompts per tool, from the free signals.
    permission_prompts: Counter = field(default_factory=Counter)
    #: Why a session ended (``session_end``), one count per session that
    #: logged one -- from the free signals (SIG-2).
    end_reasons: Counter = field(default_factory=Counter)
    #: What Claude waited for (``waits``), summed across every session --
    #: from the free signals (SIG-2). Includes the same ``quota`` count
    #: ``limits.signals_cross_check`` reads independently.
    waits: Counter = field(default_factory=Counter)
    #: Skills Claude has loaded, so a slash command can be told from one.
    skill_names: set = field(default_factory=set)
    commands_run: Counter = field(default_factory=Counter)
    #: Plan check answers, by ``(plan_shape of the session, word)``: the
    #: other half of the plan answers the ``habits_by_shape`` columns count
    #: (a piece's own plan answer is in :attr:`Piece.plan`; /cg-feedback asks
    #: it only when the plan check wasn't asked, so the two never overlap).
    plan_checks: Counter = field(default_factory=Counter)
    #: Thinking share of output (percent) before ``effort_fit`` fires --
    #: same default as ``recommend.RecommendThresholds
    #: .effort_mismatch_thinking_share_pct``; ``build_section`` resolves
    #: the configured value so the two never disagree (UX-3).
    effort_share_threshold_pct: float = 30.0
    #: A known token saver (tokensave) was relied on in the window: its
    #: own calls and the calls its hook turned away were at least
    #: ``known_savers.ADVICE_MIN_SHARE`` of every session's tool calls
    #: together (``saver_calls``/``tool_calls``). ``explore_research``
    #: reads this to switch its advice from an Explore agent -- which the
    #: saver's hook would just block -- to the saver's own search tools.
    saver_active: bool = False
    saver_calls: int = 0
    tool_calls: int = 0
    #: The zone weeks and days were counted in (``config.tz``; ``None`` is
    #: the machine's own zone), and the report's window label
    #: (``ReportMeta.window``: "last 7 days", "all time", "since ... until
    #: now") that ``digest_table`` titles itself with. ``""`` when the
    #: caller didn't say which window this is.
    tz: str | tzinfo | None = None
    window: str = ""
    #: When metrics capture was turned on (``config.capture.enabled_at``,
    #: an ISO time; ``""`` when unknown): the weeks before it have no tags
    #: to measure, and the tagged share counts from it on, as the Capture
    #: banner does.
    since: str = ""

    @property
    def weeks(self) -> list[str]:
        return sorted({c.week for c in self.cycles if c.week})

    @property
    def span_days(self) -> float:
        """How many days the messages cover (a day at least)."""
        moments = [c.ts for c in self.cycles if c.ts is not None]
        if not moments:
            return 1.0
        return max(1.0, (max(moments) - min(moments)).total_seconds() / 86400)

    @property
    def span_weeks(self) -> float:
        """How many weeks the messages cover, for spreading a total into a
        weekly rate -- never less than a full week (UX-4/7, F3: this used
        to divide by a fraction of a week for a corpus under 7 days old,
        e.g. ``1 / 7`` for one day, which *multiplies* that one day's total
        by about 7x to fake a weekly rate from a single day of noise).
        Under 7 days, this is 1.0: dividing by it shows the raw total
        observed so far, not an extrapolated "week", matching "no weekly
        figure under 7 days"."""
        return max(self.span_days, 7.0) / 7


def _week(moment: datetime | None, tz: str | tzinfo | None = None) -> str:
    """The Monday, ``YYYY-MM-DD``, of the week ``moment`` falls in. Weeks
    start at local midnight on Monday in ``tz`` (the machine's own zone
    when it is empty or can't be resolved), so a message sent late on a
    Sunday evening belongs to the week its local day does, not to the
    next one because it is already Monday in UTC."""
    if moment is None:
        return ""
    day = to_local(moment, tz).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _moment(ts: str | None) -> datetime | None:
    moment = _parse_ts(ts) if ts else None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


#: Which answer stands for a session with several plan builds, worst first:
#: a piece counts once, so the answer that says something went wrong wins.
_WORST_PLAN = ("gap", "new", "covered")
_WORST_HANDOFF = ("no", "partly", "yes")


def _worst(words, order: tuple[str, ...]) -> str | None:
    held = {w for w in words if w in order}
    return next((w for w in order if w in held), None)


def _rating_feedback(rating) -> Feedback | None:
    """Your dashboard rating of a session (``Store.feedback``) as the same
    :class:`Feedback` a /cg-feedback run gives. A session with several plan
    builds answers the plan and handoff questions for each
    (``rating["builds"]``); one answer stands for the piece."""
    if not isinstance(rating, dict) or not rating.get("outcome"):
        return None
    builds = [b for b in rating.get("builds") or () if isinstance(b, dict)]
    plan, handoff = rating.get("plan"), rating.get("handoff")
    if len(builds) > 1:
        plan = _worst((b.get("plan") for b in builds), _WORST_PLAN) or plan
        handoff = _worst((b.get("handoff") for b in builds), _WORST_HANDOFF) or handoff
    return with_older_why(
        Feedback(
            outcome=rating.get("outcome"),
            slow=tuple(rating.get("slow") or ()),
            worth=rating.get("worth"),
            helped=tuple(rating.get("helped") or ()),
            handoff=handoff,
            why=tuple(rating.get("why") or ()),
            missed_in=rating.get("missed_in"),
            plan=plan,
            tip=rating.get("tip"),
            tip_hint=rating.get("tip_hint"),
            source="rating",
        )
    )


def _dominant(values) -> str | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def _session(bundle, rates: _Rates, out: Habits, rating) -> None:
    top = bundle.top
    turns = capture_mod._priced(top)
    if not turns:
        return
    own, total = known_savers.calls_in_turns(turns)
    out.saver_calls += own
    out.tool_calls += total
    out.saver_active = out.saver_calls > 0 and out.saver_calls >= known_savers.ADVICE_MIN_SHARE * out.tool_calls
    workflows = getattr(bundle, "workflows", ())
    cycles = capture_mod.prompt_cycles(top, bundle.subs, workflows)
    carry = _CarryCost(turns, rates)
    first_turn = turns[0]
    baseline = starting_context(turns)

    rated: dict[int, Feedback] = {}
    #: cycle -> the feedback span that rates it, so a follow-up is only read
    #: against the answers about its own piece.
    span_of: dict[int, int] = {}
    #: The first message of each piece whose follow-ups were things left out.
    left_out: set[int] = set()
    #: The messages of pieces check_work counts from your answers.
    missed_pieces: set[int] = set()
    spans = capture_mod.feedback_spans(cycles)
    for n, span in enumerate(spans):
        if span.feedback.source != "skipped" and span.feedback.outcome and span.cycles:
            for cycle in span.cycles:
                rated[id(cycle)] = span.feedback
                span_of[id(cycle)] = n
            if "left_out" in span.feedback.why:
                left_out.add(id(span.cycles[0]))
            piece = _piece(span.feedback, span.cycles, rates, "your feedback")
            out.pieces.append(piece)
            if "missed" in piece.reasons() or piece.plan == "covered":
                missed_pieces.update(id(c) for c in span.cycles)
    session_rating = _rating_feedback(rating)
    work = [c for c in cycles if not capture_mod.is_feedback_run(c)]
    if session_rating is not None and work and rated:
        # A /cg-feedback run already answered for this session's builds: the
        # dashboard's plan and handoff answers would count them twice.
        out.pieces.append(_piece(replace(session_rating, handoff=None, plan=None), work, rates, "dashboard rating"))
    elif session_rating is not None and work:
        # The dashboard rating is the only answer about this session's work:
        # it rates every message, as a /cg-feedback run rates its piece.
        piece = replace(_piece(session_rating, work, rates, "dashboard rating"), alone=True)
        out.pieces.append(piece)
        for cycle in work:
            rated[id(cycle)] = session_rating
            span_of[id(cycle)] = -1
        if "left_out" in session_rating.why:
            left_out.add(id(work[0]))
        if "missed" in piece.reasons() or piece.plan == "covered":
            missed_pieces.update(id(c) for c in work)
    shape = plan_shape(turns)
    if work:
        out.shapes.append(
            SessionShape(
                shape=shape,
                cost=sum(capture_mod._cycle_cost(c, rates.pricing) for c in cycles),
                carried=plan_carried(turns),
            )
        )

    rework, planned_after = _pieces_of_session(
        bundle.session_id, cycles, rates, out, spans, session_rating, rated, shape
    )

    index_of = {id(t): i for i, t in enumerate(turns)}
    denied = _denials(top, turns)
    first_read: dict[str, int] = {}
    for i, turn in enumerate(turns):
        for h in turn.read_target_hashes:
            first_read.setdefault(h, i)
        for name in turn.skills_invoked:
            out.skill_names.add(name)
        for name in turn.commands_run:
            out.commands_run[name] += 1

    facts: list[CycleFact] = []
    for cycle in work:
        facts.append(
            _cycle_fact(
                bundle.session_id, cycle, carry, index_of, rates, baseline, rated, session_rating, denied, out.tz
            )
        )
    captured = capture_mod.is_captured(top)
    begun = capture_mod._start(out.since)
    #: The nearest cycle before this one that was not rework: the work the
    #: rework cycles after it are redoing.
    anchor = 0
    for n, (fact, cycle) in enumerate(zip(facts, work)):
        fact.left_out = id(cycle) in left_out
        fact.in_missed_piece = id(cycle) in missed_pieces
        fact.tokens = _cycle_tokens(cycle)
        fact.asks = pieces_mod.asks(cycle, work[n - 1] if n else None)
        fact.planned = fact.planned or id(cycle) in planned_after
        fact.executes_plan = id(cycle) in planned_after or _carries_out_plan(cycle)
        fact.outside_capture = (
            not captured
            or (begun is not None and (fact.ts is None or fact.ts < begun))
            or capture_mod._excluded_from_coverage(cycle, turns)
        )
        word = fact.plan_check
        if word in _PLAN_ANSWERS:
            out.plan_checks[(shape, word)] += 1
        if not n:
            continue
        tag = cycle.settled
        reads_as_redo = (tag is not None and tag.shift in ("redo", "fix")) or cycle.turns[0].human_correction
        if reads_as_redo and _not_rework(work[anchor], cycle, rated, span_of):
            facts[anchor].excused = True
        elif reads_as_redo or id(cycle) in rework:
            # Each cycle of a run of rework adds its cost, once, to the one
            # delivery before the run; none of them is redone itself.
            facts[anchor].redone = True
            facts[anchor].redo_cost += capture_mod._cycle_cost(cycle, rates.pricing)
            continue
        anchor = n
    out.cycles.extend(facts)

    if len(work) == 1 and not work[0].subs and len(work[0].turns) <= 3:
        tag = work[0].settled
        if tag is None or tag.size in (None, "xs", "s"):
            premium = first_turn.cache_creation_tokens * max(0.0, rates.write(first_turn) - rates.read(first_turn))
            day = local_day(facts[0].ts, out.tz) if facts and facts[0].ts else ""
            out.small_sessions.append((bundle.session_id, day, bundle.slug, premium, tag is not None))

    _agents(bundle, cycles, turns, carry, first_read, rates, out, workflows)


def _carries_out_plan(cycle) -> bool:
    """Whether ``cycle`` carried out a plan: a reply of it had its plan
    approved and more replies followed (the dialog's approval: the build is
    the rest of the cycle), or Claude's tag says it followed or departed
    from one."""
    at = next((n for n, t in enumerate(cycle.turns) if capture_mod._plan_approved(t)), None)
    if at is not None and at + 1 < len(cycle.turns):
        return True
    tag = cycle.settled
    return tag is not None and tag.plan in ("following", "deviated")


def _pieces_of_session(
    session_id: str, cycles: list, rates: _Rates, out: Habits, spans: list, session_rating, rated: dict, shape: str
) -> tuple[set[int], set[int]]:
    """Draw the session's pieces of work (``pieces.pieces_of``) and record
    what they say: a :class:`Piece` for each one that no kept answer or
    rating covers, and a :class:`PlanFix` for each with an approved plan.
    Returns ``(ids of the rework cycles, ids of the cycles that came after
    a plan you approved by typing, up to the end of their piece)``."""
    found = pieces_mod.pieces_of(
        cycles, rates, session_rating if session_rating is not None and not spans else spans, session_id=session_id
    )
    rework: set[int] = set()
    planned_after: set[int] = set()
    for work_piece in found:
        out.work_pieces.append(work_piece)
        mine = [cycles[i] for _, i in work_piece.cycle_ids]
        rework.update(id(cycles[i]) for _, i in work_piece.rework_ids)
        if session_rating is None and not any(id(c) in rated for c in mine):
            out.pieces.append(
                replace(
                    _piece(Feedback(), mine, rates, "transcript"),
                    substantive=work_piece.substantive,
                    rework=work_piece.rework,
                    rework_cost=work_piece.rework_cost,
                )
            )
        typed_go = False
        for cycle in mine:
            if typed_go:
                planned_after.add(id(cycle))
            typed_go = typed_go or any(
                t.plan_stats is not None and t.plan_stats.outcome == "approved_by_message" for t in cycle.turns
            )
        fix = _plan_fix(mine, shape, rates)
        if fix is not None:
            out.plan_fixes.append(fix)
    return rework, planned_after


def _plan_fix(cycles: list, shape: str, rates: _Rates) -> PlanFix | None:
    """The fixes after the first plan approved in a piece's ``cycles``:
    corrections and adjustments you typed, and those you typed while Claude
    worked (``Turn.queued_correction`` and ``queued_adjust``, one count per
    reply). A message in plan mode is a reply to a plan, no fix, and neither
    is one you told the plan check was not a fix. ``None`` when no plan was
    approved, or nothing came after it."""
    for k, cycle in enumerate(cycles):
        at = next((n for n, t in enumerate(cycle.turns) if capture_mod._plan_approved(t)), None)
        if at is not None:
            break
    else:
        return None
    if at + 1 >= len(cycle.turns) and k + 1 >= len(cycles):
        return None
    fix = PlanFix(shape=shape)
    # The rest of the cycle that put the plan up is the build, when the
    # dialog approved it: only what you typed while it ran can be a fix.
    held = [(cycle, cycle.turns[at + 1 :], False)]
    held += [(c, c.turns, not c.turns[0].prompt_plan_mode) for c in cycles[k + 1 :]]
    for c, turns, opening in held:
        queued = sum(1 for t in turns if t.queued_correction or t.queued_adjust)
        typed = int(
            opening and (c.turns[0].human_correction or c.turns[0].human_adjust) and _plan_word(c) != "none"
        )
        fix.typed += typed
        fix.queued += queued
        if typed or queued:
            fix.cost += capture_mod._cycle_cost(c, rates.pricing)
    return fix


def fixes_after_plan(h: Habits, shape: str | None = None) -> PlanFixes | None:
    """How often the plans you approved had to be fixed: of the pieces of
    work with an approved plan (those of one ``shape``, or all), how many
    had :data:`PLAN_FIXES_MIN` or more corrections and adjustments after it,
    counting the ones you typed while Claude worked. ``None`` with fewer
    than :data:`MIN_GROUP` plans: too few to say anything. The ``plan_build``
    row of ``habits_by_shape`` carries the same figures."""
    plans = [p for p in h.plan_fixes if shape is None or p.shape == shape]
    if len(plans) < MIN_GROUP:
        return None
    fixed = [p for p in plans if p.fixes >= PLAN_FIXES_MIN]
    return PlanFixes(
        plans=len(plans),
        fixed=len(fixed),
        fixes=sum(p.fixes for p in plans),
        queued=sum(p.queued for p in plans),
        cost=sum(p.cost for p in fixed),
    )


def _denials(top, turns: list[Turn]) -> dict[int, list[str]]:
    """Each stopped tool call's bucket word, by the reply that came after
    it."""
    stamps = [t.ts or "" for t in turns]
    out: dict[int, list[str]] = {}
    for event in top.events:
        if event.kind != EventKind.TOOL_DENIAL or not event.ts:
            continue
        i = bisect.bisect_left(stamps, event.ts)
        if i < len(turns):
            out.setdefault(i, []).append(events_mod.denial_bucket_of(event))
    return out


#: The plan answers that say something about the plan (``none`` says the
#: message wasn't a fix).
_PLAN_ANSWERS = ("covered", "gap", "new")


def _plan_word(cycle) -> str:
    """The plan check answer on ``cycle``'s replies, or ``""``."""
    return next((t.plan_check.word for t in cycle.turns if t.plan_check is not None and t.plan_check.word), "")


def _not_rework(cycle, following, rated: dict, span_of: dict) -> bool:
    """Whether what you said about ``following`` rules it out as a redo of
    ``cycle``'s work, though it reads like one: the plan check says it was
    new, or your feedback on their piece says the plan check missed
    nothing (``plan=new``) or the follow-ups were a change of mind alone.
    A mix of reasons rules nothing out: which follow-ups were which isn't
    known."""
    if _plan_word(following) == "new":
        return True
    n = span_of.get(id(following))
    fb = rated.get(id(following))
    if fb is None or n is None or span_of.get(id(cycle)) != n:
        return False
    if fb.plan == "new":
        return True
    return {w for w in fb.why if w != "none"} == {"changed"}


def _cycle_tokens(cycle) -> int:
    """Every token a message's work used, subagents included: input, cache
    writes, cache reads and output, with a reply to an agent's report
    counted for the message that started the agent (``cycle_spend``)."""
    spend = capture_mod.cycle_spend(cycle, None)
    return spend.tokens + spend.agent_tokens


def _followups(cycles) -> list:
    """The follow-up messages of a piece: every one after the first that
    isn't a go-ahead or a status check, as the hook counts them."""
    return [c for c in cycles[1:] if not (c.turns[0].human_go or c.turns[0].human_status)]


def _piece(fb: Feedback, cycles, rates: _Rates, source: str) -> Piece:
    tags = [c.settled for c in cycles if c.settled is not None]
    follow = _followups(cycles)
    return Piece(
        outcome=fb.outcome,
        cost=sum(capture_mod._cycle_cost(c, rates.pricing) for c in cycles),
        cycles=len(cycles),
        task=_dominant(t.task for t in tags),
        slow=tuple(w for w in fb.slow if w != "none"),
        helped=tuple(w for w in fb.helped if w != "none"),
        source=source,
        shape=plan_shape([t for c in cycles for t in c.turns]),
        worth=fb.worth,
        handoff=fb.handoff,
        # An older run's ``slow`` also gave ``why``: count it once.
        why=() if fb.why_older else tuple(w for w in fb.why if w != "none"),
        missed_in=fb.missed_in,
        plan=fb.plan,
        tip=fb.tip,
        tip_hint=fb.tip_hint,
        followups=len(follow),
        followup_cost=sum(capture_mod._cycle_cost(c, rates.pricing) for c in follow),
        followup_tokens=sum(_cycle_tokens(c) for c in follow),
        plan_checks=tuple(w for w in (_plan_word(c) for c in cycles) if w),
        why_given=bool(fb.why or fb.slow),
        helped_given=bool(fb.helped),
    )


def _cycle_fact(
    session_id, cycle, carry: _CarryCost, index_of, rates: _Rates, baseline, rated, session_rating, denied, tz=None
):
    first = cycle.turns[0]
    moment = _moment(first.ts)
    start_ctx = first.ctx - (first.human_prompt_chars or 0) // capture_mod.CHARS_PER_TOKEN
    stale = max(0, start_ctx - baseline)
    idx = [index_of[id(t)] for t in cycle.turns]
    fact = CycleFact(
        session_id=session_id,
        ts=moment,
        week=_week(moment, tz),
        cost=capture_mod._cycle_cost(cycle, rates.pricing),
        turns=len(cycle.turns),
        tag=cycle.settled,
        written=cycle.tag,
        flags=tuple(first.prompt_flags),
        paste=first.human_prompt_has_paste,
        stale_tokens=stale,
        stale_cost=stale * sum(carry.reads[i] for i in idx),
        stale_rewrite=stale * carry.writes[idx[0]],
        gap_s=first.gap_s,
        quiet=first.human_go or first.human_status or first.human_ack,
        limit_pause=first.gap_cause == "limit",
        # The dominant effort across the cycle's own turns, not just the
        # first reply's (F6): a message answered over several turns can
        # change effort mid-way, and the first turn alone isn't
        # representative of what the message as a whole ran at.
        effort=_dominant(t.effort for t in cycle.turns) or first.effort,
        model=_dominant(t.model for t in cycle.turns) or "",
        speed=_dominant(t.speed for t in cycle.turns),
        main_cost=sum(rates.cost(t) for t in cycle.tag_turns),
        growth_cost=sum(max(0, t.ctx - first.ctx) * carry.reads[i] for t, i in zip(cycle.turns, idx)),
        explore_agents=sum(
            1 for sub in cycle.subs if sub.meta.agent_type == _EXPLORE_AGENT and sub.meta.kind != "workflow-agent"
        ),
    )
    fb = rated.get(id(cycle)) or session_rating
    if fb is not None:
        fact.outcome = fb.outcome
        fact.outcome_source = fb.source
        fact.worth = fb.worth
        fact.helped = tuple(w for w in fb.helped if w != "none")
    fact.plan_check = _plan_word(cycle)
    for n, (turn, i) in enumerate(zip(cycle.turns, idx)):
        fact.reads += sum(turn.tool_calls_by_tool.get(tool, 0) for tool in _READ_TOOLS)
        tokens = sum(turn.tool_result_chars_by_tool.get(tool, 0) for tool in _READ_TOOLS) // capture_mod.CHARS_PER_TOKEN
        fact.read_tokens += tokens
        fact.read_carry += carry.cost(i, tokens)
        for tool, chars in turn.tool_result_chars_by_tool.items():
            big = chars // capture_mod.CHARS_PER_TOKEN
            if big >= BIG_OUTPUT_TOKENS and tool not in _REPORT_TOOLS:
                fact.big_outputs.append((tool, big, carry.cost(i, big)))
        if (turn.plan_stats is not None or turn.tool_calls_by_tool.get("ExitPlanMode")) and not fact.planned:
            fact.planned = True
            fact.plan_cost = sum(carry.costs[j] for j in idx[: n + 1])
        if turn.tests_run or (
            turn.cmd_prefix
            and any(tool in turn.tool_names for tool in _SHELL_TOOLS)
            and classify._matches_test_tool(turn.cmd_prefix)
        ):
            fact.checked_by_tool = True
        by_you = set(first.commands_run)
        for name in turn.skills_invoked:
            fact.skill_calls.append((name, name in by_you, n, sum(carry.costs[j] for j in idx[:n])))
        if n and _compacted(turn):
            fact.compactions += 1
        for kind in denied.get(i, ()):
            if kind in _BLOCKED:
                fact.blocked += 1
                fact.blocked_cost += carry.costs[i]
            elif kind in _REFUSED:
                fact.refused += 1
                fact.refused_cost += carry.costs[i]
        output = rates.output(turn)
        fact.output_cost += turn.output_tokens * output
        fact.thinking_cost += turn.thinking_tokens * output
    if fact.tag is not None and fact.tag.plan in ("made", "following", "deviated"):
        fact.planned = True
    return fact


def _agents(bundle, cycles, turns, carry: _CarryCost, first_read, rates: _Rates, out: Habits, workflows=()) -> None:
    carries = {id(bundle.top): carry}
    reports: dict[str, tuple[_CarryCost, int, int]] = {}
    for i, turn in enumerate(turns):
        for use_id, chars in turn.agent_result_chars.items():
            reports[use_id] = (carry, i, chars)
    sub_turns = {}
    for sub in bundle.subs:
        priced = capture_mod._priced(sub)
        sub_turns[id(sub)] = priced
        sub_carry = _CarryCost(priced, rates)
        carries[id(sub)] = sub_carry
        for i, turn in enumerate(priced):
            for use_id, chars in turn.agent_result_chars.items():
                reports[use_id] = (sub_carry, i, chars)
    main_spawn = {}
    for i, turn in enumerate(turns):
        for use_id in turn.tool_use_ids:
            main_spawn[use_id] = i
    by_agent = {agent_key(sub.meta.agent_id): sub for sub in bundle.subs if sub.meta.agent_id}
    cycle_of = {id(sub): cycle for cycle in cycles for sub in cycle.subs}
    launches = capture_mod.WorkflowLaunches(bundle.top, workflows)

    for sub in bundle.subs:
        priced = sub_turns[id(sub)]
        if not priced:
            continue
        sub_carry = carries[id(sub)]
        first = priced[0]
        cycle = cycle_of.get(id(sub))
        fact = AgentFact(
            session_id=bundle.session_id,
            agent_type=sub.meta.agent_type or "general-purpose",
            week=_week(_moment(first.ts), out.tz),
            cost=sum(sub_carry.costs),
            depth=max(1, sub.meta.spawn_depth or 1),
            model=_dominant(t.model for t in priced) or "",
            direct=sub.meta.kind != "workflow-agent",
            context_tokens=sum(t.ctx for t in priced),
            capped="short" in first.prompt_flags,
            retry=first.retry_marker,
            spawn=first.spawn_marker,
            level=cycle.settled.level if cycle is not None and cycle.settled is not None else None,
            task=cycle.settled.task if cycle is not None and cycle.settled is not None else None,
        )
        for turn in reversed(priced):
            if fact.result is None and turn.result_marker:
                fact.result = turn.result_marker
            cap = turn.cap
            if cap is not None:
                fact.rules = fact.rules or cap.rules
                fact.brief = fact.brief or cap.brief
                fact.missing = fact.missing or tuple(cap.missing)
        fact.calls = len(priced)
        fact.probe_calls = sum(1 for turn in priced if _is_probe(turn))
        fact.calls_before_edit = _calls_before_edit(priced)
        found = reports.get(sub.meta.tool_use_id or "")
        if found is not None:
            parent_carry, i, chars = found
            fact.report_tokens = chars // capture_mod.CHARS_PER_TOKEN
            fact.report_carry = parent_carry.cost(i, fact.report_tokens)
        spawn_at = _spawn_index(sub, main_spawn, by_agent, launches)
        if spawn_at is not None:
            for i, turn in enumerate(priced):
                hashes = turn.read_target_hashes
                again = sum(1 for h in hashes if first_read.get(h, spawn_at) < spawn_at)
                if not again:
                    continue
                per_read = turn.tool_result_chars_by_tool.get("Read", 0) / max(1, len(hashes))
                fact.overlap_reads += again
                fact.overlap_cost += sub_carry.cost(i, again * per_read / capture_mod.CHARS_PER_TOKEN)
        out.agents.append(fact)


def _is_probe(turn) -> bool:
    """Whether a reply (one message id) only looked, once: its one tool call
    was a Read, Grep or Glob, or a shell command that reads files
    (``shell_reads``: grep, sed -n, cat, find, git log) and writes or moves
    none. A reply with two calls, or with a call that did anything else,
    is not. What a small model does as well as a large one."""
    calls = turn.tool_calls_by_tool
    if sum(calls.values()) != 1:
        return False
    if any(calls.get(tool) for tool in _READ_TOOLS):
        return True
    return (
        any(calls.get(tool) for tool in _SHELL_TOOLS)
        and turn.shell_read_count > 0
        and not (turn.shell_write_count or turn.shell_change_count)
    )


def _calls_before_edit(turns) -> int | None:
    """How many replies a run made before the first one that changed your
    files (an edit call, or a shell command that wrote or moved them): 0
    when its first reply did. ``None`` when it changed none."""
    for n, turn in enumerate(turns):
        if turn.edit_call_count or turn.shell_write_count or turn.shell_change_count:
            return n
    return None


def _spawn_index(sub, main_spawn, by_agent, launches=None) -> int | None:
    """The main-session reply that started ``sub``, or its top-level
    ancestor for a nested spawn. A workflow agent has no tool call of its
    own: ``launches`` (``capture.WorkflowLaunches``) finds the reply that
    started or resumed its run, by ``meta.kind``, never by agent type."""
    seen = set()
    while sub is not None and id(sub) not in seen:
        seen.add(id(sub))
        if sub.meta.tool_use_id in main_spawn:
            return main_spawn[sub.meta.tool_use_id]
        if launches is not None and sub.meta.kind == "workflow-agent":
            return launches.turn_index(sub)
        sub = by_agent.get(agent_key(sub.meta.parent_agent_id)) if sub.meta.parent_agent_id else None
    return None


def collect(
    corpus,
    pricing: Pricing | None,
    *,
    ratings: dict | None = None,
    signals: dict | None = None,
    effort_share_threshold_pct: float = 30.0,
    tz: str | tzinfo | None = None,
    window: str = "",
    since: str = "",
) -> Habits:
    """Work out the facts for every session in ``corpus``. ``ratings``
    holds your dashboard ratings by session id (``Store.all_feedback``);
    ``signals`` the free signals by session id (``signals.by_session``).
    ``effort_share_threshold_pct`` is ``effort_fit``'s share gate -- see
    ``Habits.effort_share_threshold_pct``. ``tz`` (``config.tz``; the
    machine's own zone when empty) is the zone weeks and days are counted
    in, and ``window`` the report's window label (``ReportMeta.window``),
    which titles ``digest_table`` -- see ``Habits.tz``. ``since``
    (``config.capture.enabled_at``) is when metrics capture was turned on:
    see ``Habits.since``."""
    out = Habits(effort_share_threshold_pct=effort_share_threshold_pct, tz=tz, window=window, since=since)
    rates = _Rates(pricing)
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        _session(bundle, rates, out, (ratings or {}).get(bundle.session_id))
    for seen in (signals or {}).values():
        out.permission_prompts.update(seen.permission_prompts)
        out.waits.update(seen.waits)
        if seen.end_reason:
            out.end_reasons[seen.end_reason] += 1
    return out


# -- the playbook --------------------------------------------------------------


@dataclass(slots=True)
class Item:
    key: str
    saving: float | None
    n: int
    sources: tuple[str, ...]
    evidence: str
    example: str = ""
    #: Overrides ``ITEMS[key][1]`` for this item's own report, when a
    #: finding changes what the habit itself asks for (so far only
    #: ``explore_research``, switched to tokensave's tools -- see
    #: ``item_title``); "" to use the fixed title.
    title: str = ""
    #: Same override shape as ``title``, for the playbook row's
    #: ``WHERE``/``TRADE_OFFS`` entries (``playbook_table`` uses
    #: ``item.where or WHERE[item.key]``, etc.); "" to use the fixed one.
    where: str = ""
    trade_off: str = ""
    undo: str = ""
    #: week -> USD the habit addresses, for the trend.
    waste: dict = field(default_factory=dict)
    #: EST-P7: the share of ``saving`` that comes from a ``reported`` or
    #: ``your feedback`` row, rather than one merely inferred from the
    #: transcript's shape -- 1.0 when every dollar needs capture (or your
    #: feedback) to exist, 0.0 when none of it does, and something
    #: between for an item whose evidence is a genuine mix. Unlike
    #: ``sources`` (which only says *whether* a source is present),
    #: this says *how much of the dollar figure* depends on it, so
    #: ``capture_dependent_value`` can weight instead of gating
    #: all-or-nothing (A3).
    reported_share: float = 1.0
    #: Every dollar of ``saving`` comes from a message Claude tagged, so the
    #: trend divides by the tagged messages of a week, not all of them: an
    #: untagged week would otherwise read as a week with nothing to fix.
    tagged: bool = False

    @property
    def source(self) -> str:
        return " + ".join(self.sources)


def item_title(item: Item) -> str:
    """``item``'s display title: its own :attr:`Item.title` when the
    finding switched it, else ``ITEMS[item.key]``'s fixed one."""
    return item.title or ITEMS[item.key][1]


def _by_week(pairs) -> dict[str, float]:
    out: dict[str, float] = {}
    for week, usd in pairs:
        if week:
            out[week] = out.get(week, 0.0) + usd
    return out


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _pct(part: int, whole: int) -> float | None:
    return 100.0 * part / whole if whole else None


def _sources(reported: bool, inferred: bool, feedback: bool = False) -> tuple[str, ...]:
    return tuple(s for s, on in (("reported", reported), ("inferred", inferred), ("your feedback", feedback)) if on)


def _k(tokens: float) -> str:
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else f"{tokens:.0f}"


# -- what your /cg-feedback answers say -------------------------------------------


def _answered(h: Habits) -> list[Piece]:
    """The pieces of work you rated through /cg-feedback, and the dashboard
    ratings of sessions no /cg-feedback run rated (:attr:`Piece.alone`); a
    dashboard rating beside a run would count the same follow-ups twice."""
    return [p for p in h.pieces if p.source == "your feedback" or p.alone]


def _reason_followups(h: Habits, reason: str) -> tuple[int, int]:
    """``(n, m)``: of the ``m`` follow-ups in pieces whose cause you gave,
    ``n`` were in pieces where ``reason`` was one of them. You answer what
    your follow-ups were *mostly*, so a piece's follow-ups all count for
    each reason it names."""
    given = [p for p in _answered(h) if p.why_given]
    return sum(p.followups for p in given if reason in p.reasons()), sum(p.followups for p in given)


def left_out_note(h: Habits) -> str:
    """"N of M follow-ups were things your request left out", from your
    answers, or ``""`` with fewer than
    :data:`~claudeglass.handoff.MIN_FEEDBACK_ANSWERS` pieces naming it."""
    left, followups = _reason_followups(h, "left_out")
    named = sum(1 for p in _answered(h) if p.why_given and "left_out" in p.reasons())
    if named >= MIN_FEEDBACK_ANSWERS and left:
        return f"{left} of {followups} follow-ups were things your request left out"
    return ""


def _information_notes(h: Habits) -> list[str]:
    """What your answers say about how much your first message held, as
    evidence clauses for the habits about briefing Claude: follow-ups that
    were things you hadn't said, and pieces you said more up front would
    have made cheaper. Each shows only with at least
    :data:`~claudeglass.handoff.MIN_FEEDBACK_ANSWERS` behind it."""
    notes = [note for note in (left_out_note(h),) if note]
    wanted, rated = _helped_by(h, "context")
    if wanted >= MIN_FEEDBACK_ANSWERS:
        notes.append(f"you said more in your first message would have made {wanted} of {rated} pieces cheaper")
    return notes


def _helped_by(h: Habits, word: str) -> tuple[int, int]:
    """``(n, m)``: of the ``m`` pieces where you answered what would have
    made them cheaper (Nothing included), ``n`` said ``word`` (``context``,
    ``plan`` or ``smaller``)."""
    given = [p for p in _answered(h) if p.helped_given]
    return sum(word in p.helped for p in given), len(given)


#: The longest sentence an item's evidence text runs to before it starts
#: a new one (``docs/writing-help.md``: 25 words at most).
_MAX_SENTENCE_WORDS = 25


def _clauses(parts: list[str]) -> str:
    """An item's evidence clauses as text: joined with "; " while the
    sentence stays within :data:`_MAX_SENTENCE_WORDS` words, then a new
    sentence. Each sentence starts with a capital and ends with a full
    stop."""
    sentences: list[list[str]] = []
    for part in parts:
        if sentences and len(" ".join([*sentences[-1], part]).split()) <= _MAX_SENTENCE_WORDS:
            sentences[-1].append(part)
        else:
            sentences.append([part])
    out = []
    for group in sentences:
        text = "; ".join(group)
        out.append(text[:1].upper() + text[1:] + ".")
    return " ".join(out)


def _item_split_large(h: Habits) -> Item | None:
    # (cycle, source): your own answers join the large asks Claude reported
    # or the shape implies. A message in a piece you rated too costly, or
    # said smaller pieces would have helped, is one; a large ask in a piece
    # you said was worth it is left out.
    big = []
    worth_it = 0
    for c in h.cycles:
        reported = c.tag is not None and c.tag.size in ("l", "xl")
        inferred = (c.tag is None or c.tag.size is None) and (c.compactions or c.turns >= LARGE_TURNS)
        # Your answer joins a message Claude didn't size as small: one it
        # called xs or s is no large ask, whatever the piece cost.
        small = c.tag is not None and c.tag.size in ("xs", "s")
        told = not small and (c.worth == "no" or "smaller" in c.helped)
        if (reported or inferred) and c.worth == "yes" and not told:
            worth_it += 1
        elif told:
            big.append((c, "your feedback"))
        elif reported or inferred:
            big.append((c, "reported" if reported else "inferred"))
    saving = sum(0.5 * c.growth_cost for c, _ in big)
    if not big or saving <= 0:
        return None
    typical = sorted(c.cost for c in h.cycles)[len(h.cycles) // 2]
    ratio = _mean(c.cost for c, _ in big) / typical if typical else None
    parts = [f"{len(big)} large asks"]
    if ratio:
        parts[0] += f" cost {ratio:.1f}x a typical one"
    compacted = sum(1 for c, _ in big if c.compactions)
    if compacted:
        parts.append(f"{compacted} were compacted part-way")
    redone = sum(1 for c, _ in big if c.redone)
    if redone:
        parts.append(f"{redone} had to be redone")
    costly = sum(1 for c, _ in big if c.worth == "no")
    smaller = sum(1 for c, _ in big if "smaller" in c.helped)
    if costly:
        parts.append(f"{costly} were in work you rated too costly")
    if smaller:
        parts.append(f"{smaller} were in work you said smaller pieces would have helped")
    if worth_it:
        parts.append(f"left out: {worth_it} large {'ask' if worth_it == 1 else 'asks'} you rated worth it")
    sources = {source for _, source in big}
    reported_saving = sum(0.5 * c.growth_cost for c, source in big if source != "inferred")
    return Item(
        "split_large", saving, len(big), tuple(s for s in ("reported", "inferred", "your feedback") if s in sources),
        _clauses(parts), waste=_by_week((c.week, 0.5 * c.growth_cost) for c, _ in big),
        reported_share=reported_saving / saving if saving else 0.0,
    )


def _item_batch_small(h: Habits) -> Item | None:
    groups: dict[tuple, list] = {}
    for entry in h.small_sessions:
        groups.setdefault((entry[1], entry[2]), []).append(entry)
    extra = [entry for group in groups.values() if len(group) >= 2 for entry in sorted(group)[1:]]
    saving = sum(entry[3] for entry in extra)
    if not extra or saving <= 0:
        return None
    week_of = {c.session_id: c.week for c in h.cycles}
    reported_saving = sum(entry[3] for entry in extra if entry[4])
    return Item(
        "batch_small", saving, len(h.small_sessions),
        _sources(any(e[4] for e in extra), any(not e[4] for e in extra)),
        f"{len(h.small_sessions)} sessions were one small ask each; {len(extra)} times you started another "
        "the same day in the same project, paying the start-up cost again.",
        waste=_by_week((week_of.get(e[0], ""), e[3]) for e in extra),
        reported_share=reported_saving / saving if saving else 0.0,
    )


def _item_clear_between(h: Habits) -> Item | None:
    reported, inferred = [], []
    for c in h.cycles:
        if c.stale_tokens < STALE_TOKENS:
            continue
        tag = c.tag
        # A new task that needed the earlier work (``prior`` needed or some)
        # is no reason to clear: the context was wanted.
        if tag is not None and ((tag.shift == "new" and tag.prior not in ("needed", "some")) or tag.prior == "none"):
            reported.append((c, c.stale_cost))
        elif (
            (c.gap_s or 0) >= LONG_BREAK_S
            # A thank-you, a status check or a go-ahead carries on the same work,
            # and so does a message after a usage-limit pause: neither is a new task.
            and not c.quiet
            and not c.limit_pause
            and not (tag is not None and (tag.shift in ("build", "grew", "redo", "fix") or tag.prior in ("needed", "some")))
        ):
            inferred.append((c, 0.5 * (c.stale_rewrite + c.stale_cost)))
    found = reported + inferred
    saving = sum(usd for _, usd in found)
    if not found or saving <= 0:
        return None
    parts = []
    if reported:
        avg = _mean(c.stale_tokens for c, _ in reported)
        parts.append(f"{len(reported)} new tasks began with about {_k(avg)} tokens of earlier work still in context")
    if inferred:
        avg = _mean(c.stale_tokens for c, _ in inferred)
        parts.append(
            f"{len(inferred)} messages came after a break of over an hour, with about {_k(avg)} tokens of "
            "earlier work written to the cache again"
        )
    reported_saving = sum(usd for _, usd in reported)
    # SIG-2: end_reasons is a session-level signal (once per session that
    # logged a SessionEnd), while `found` above counts individual
    # messages, so this is added as context, not folded into the count
    # or n above -- how often you *did* clear explicitly, next to how
    # often stale context carried over anyway.
    explicit_clears = h.end_reasons.get("clear", 0)
    if explicit_clears:
        parts.append(f"you cleared explicitly {explicit_clears} times")
    return Item(
        "clear_between", saving, len(found), _sources(bool(reported), bool(inferred)),
        _clauses(parts), waste=_by_week((c.week, usd) for c, usd in found),
        reported_share=reported_saving / saving if saving else 0.0,
    )


def _missing_counts(cycles) -> Counter:
    """How often each ``missing`` word was reported, a word on a message
    whose follow-ups you said were things it left out counting twice: your
    answer confirms that something was missing, so those lines rank higher."""
    counts: Counter = Counter()
    for c in cycles:
        if c.tag is not None:
            for w in c.tag.missing:
                if w != "none":
                    counts[w] += 2 if c.left_out else 1
    return counts


def _top_missing(cycles, limit: int = 2) -> list[str]:
    return [w for w, _ in _missing_counts(cycles).most_common(limit)]


#: Fewest clear asks of one kind of work and level before they are the
#: baseline for a partial or vague ask of that level; fewer, and the whole
#: kind of task is.
_BRIEF_LEVEL_MIN = 3


@dataclass(frozen=True, slots=True)
class BriefComparison:
    """What partial and vague asks cost against clear ones of the same kind
    of task (:func:`brief_comparison`)."""

    #: Partial or vague messages compared, clear ones compared against, and
    #: the kinds of task they come from.
    unclear: int
    clear: int
    tasks: int
    #: The chance that a partial or vague ask cost more than a clear one of
    #: its kind, and how much more the median one cost.
    probability: float
    ratio: float | None
    #: ``(message, USD)``: half of how far each partial or vague ask went
    #: over, or under, the median clear ask of its kind and level.
    gaps: tuple = ()

    @property
    def saving(self) -> float:
        return sum(usd for _, usd in self.gaps)

    @property
    def holds(self) -> bool:
        """Partial and vague asks cost more, often enough to say so, and in
        all."""
        return self.probability >= BRIEF_MIN_PROBABILITY and self.saving > 0


def _brief_cost(c: CycleFact) -> float:
    """What a message's own work cost: its cost less re-reading the context
    it began with, so a message late in a long session isn't dearer for
    where it sat."""
    return max(0.0, c.cost - c.stale_cost)


def brief_comparison(h: Habits) -> BriefComparison | None:
    """Partial and vague asks against clear ones, like for like: within one
    kind of task, with at least :data:`MIN_GROUP` messages on each side, and
    leaving out the messages that carried out a plan, whose cost is the
    plan's build, not the ask. Each unclear ask is set against the median
    clear ask of its level (the median clear ask of the task when fewer
    than three share its level), at what the work itself cost
    (:func:`_brief_cost`). ``None`` when no kind of task has enough on both
    sides."""
    kinds: dict[str, tuple[list, list]] = {}
    for c in h.cycles:
        if c.tag is None or not c.tag.brief or not c.tag.task or c.executes_plan:
            continue
        clear, unclear = kinds.setdefault(c.tag.task, ([], []))
        (clear if c.tag.brief == "clear" else unclear).append(c)
    gaps, baselines, tasks = [], [], 0
    wins, pairs, clear_n = 0.0, 0, 0
    for clear, unclear in kinds.values():
        if len(clear) < MIN_GROUP or len(unclear) < MIN_GROUP:
            continue
        tasks += 1
        clear_n += len(clear)
        everyone = [_brief_cost(c) for c in clear]
        by_level: dict[str, list[float]] = {}
        for c in clear:
            by_level.setdefault(c.tag.level or "", []).append(_brief_cost(c))
        for c in unclear:
            same = by_level.get(c.tag.level or "", [])
            costs = same if len(same) >= _BRIEF_LEVEL_MIN else everyone
            base, cost = statistics.median(costs), _brief_cost(c)
            gaps.append((c, 0.5 * (cost - base)))
            baselines.append(base)
            wins += sum((cost > k) + 0.5 * (cost == k) for k in costs)
            pairs += len(costs)
    if not gaps:
        return None
    middle = statistics.median(baselines)
    ratio = statistics.median(_brief_cost(c) for c, _ in gaps) / middle if middle else None
    return BriefComparison(
        unclear=len(gaps), clear=clear_n, tasks=tasks, probability=wins / pairs, ratio=ratio, gaps=tuple(gaps),
    )


def brief_card_shown(h: Habits, items: list[Item] | None = None) -> bool:
    """Whether Work habits shows the ``brief_clearly`` card: partial and
    vague asks cost more than clear ones like for like, or your own answers
    say your requests left things out, and the habit isn't one you already
    picked up. /cg-brief is offered only while it does. ``items`` is
    :func:`playbook`'s, when the caller has it."""
    items = playbook(h) if items is None else items
    return any(item.key == "brief_clearly" for item in _worth_trying(h, items))


def _item_brief_clearly(h: Habits) -> Item | None:
    tagged = [c for c in h.cycles if c.tag is not None and c.tag.brief]
    unclear = [c for c in tagged if c.tag.brief in ("partial", "vague")]
    # Your own answers can stand in for Claude's tags: the follow-ups you
    # said were things your request left out.
    notes = _information_notes(h)
    told = bool(notes)
    left, _ = _reason_followups(h, "left_out")
    found = brief_comparison(h)
    compared = found if found is not None and found.holds else None
    if compared is None and not told:
        return None
    parts = []
    if compared is not None:
        parts.append(f"{len(unclear)} of {len(tagged)} asks were partial or vague")
        if compared.ratio:
            parts[0] += f"; the median one cost {compared.ratio:.1f}x a clear ask of the same kind"
    missing = _top_missing([*unclear, *(c for c in h.cycles if c.left_out and c not in unclear)])
    if missing:
        parts.append("most often missing: " + ", ".join(MISSING_LINES[w][0].lower() for w in missing if w in MISSING_LINES))
    parts.extend(notes)
    example = EXAMPLES["brief_clearly"]
    if missing and missing[0] in MISSING_LINES:
        example = MISSING_LINES[missing[0]][1]
    weeks = _by_week((c.week, usd) for c, usd in compared.gaps) if compared is not None else {}
    return Item(
        "brief_clearly", compared.saving if compared is not None else None,
        len(unclear) if compared is not None else max(left, _helped_by(h, "context")[0]),
        _sources(compared is not None, False, told), _clauses(parts),
        example=example, waste={w: max(0.0, usd) for w, usd in weeks.items()}, tagged=True,
    )


def _item_name_files(h: Habits) -> Item | None:
    looked = [c for c in h.cycles if c.reads]
    named = [c for c in looked if "path" in c.flags]
    unnamed = [c for c in looked if "path" not in c.flags]
    if len(named) < MIN_GROUP or len(unnamed) < MIN_GROUP:
        return None
    with_path = _mean(c.read_carry for c in named)
    gaps = [(c, 0.5 * max(0.0, c.read_carry - with_path)) for c in unnamed]
    saving = sum(usd for _, usd in gaps)
    reads_named = _mean(c.reads for c in named)
    reads_unnamed = _mean(c.reads for c in unnamed)
    if saving <= 0 or reads_unnamed <= reads_named:
        return None
    return Item(
        "name_files", saving, len(unnamed), ("inferred",),
        f"Asks that named a file ran {reads_named:.1f} reads and searches on average; the {len(unnamed)} that "
        f"didn't ran {reads_unnamed:.1f}.",
        waste=_by_week((c.week, usd) for c, usd in gaps),
        reported_share=0.0,
    )


def _item_paste_errors(h: Habits) -> Item | None:
    bugs = [c for c in h.cycles if c.tag is not None and c.tag.task in ("bugfix", "debug")]
    with_error = [c for c in bugs if "error" in c.flags or c.paste]
    without = [c for c in bugs if not ("error" in c.flags or c.paste)]
    if len(with_error) < 3 or len(without) < 3:
        return None
    avg_with = _mean(c.cost for c in with_error)
    avg_without = _mean(c.cost for c in without)
    if not avg_with or avg_without <= avg_with:
        return None
    gaps = [(c, 0.5 * max(0.0, c.cost - avg_with)) for c in without]
    return Item(
        "paste_errors", sum(usd for _, usd in gaps), len(without), ("reported", "inferred"),
        f"{len(without)} bug reports came without the error or its output; they cost {avg_without / avg_with:.1f}x "
        "the ones that had it.",
        waste=_by_week((c.week, usd) for c, usd in gaps),
        reported_share=0.5, tagged=True,
    )


def _item_explore_research(h: Habits) -> Item | None:
    heavy = [c for c in h.cycles if c.reads >= RESEARCH_READS and not c.explore_agents and c.read_tokens]
    gaps = [
        (c, c.read_carry * max(0, c.read_tokens - EXPLORE_REPORT_TOKENS) / c.read_tokens) for c in heavy
    ]
    saving = sum(usd for _, usd in gaps)
    if not heavy or saving <= 0:
        return None
    # Tokensave's hook blocks every Explore agent call in an indexed
    # project (``known_savers``), so when it was at work in the window
    # the advice switches to its own search tools instead.
    tokensave = h.saver_active
    with_no = "with tokensave's own tools available instead" if tokensave else "with no Explore agent"
    parts = [f"{len(heavy)} asks ran {_mean(c.reads for c in heavy):.0f} reads and searches in the main session on "
             f"average, {with_no}"]
    # CAP-5: found=no|partial on one of these *heavy* cycles is direct
    # evidence its reading didn't pay off, so it -- not an unrelated count
    # over the whole corpus (A4) -- decides which dollars of ``saving``
    # count as reported.
    confirmed = sum(usd for c, usd in gaps if c.tag is not None and c.tag.found in ("no", "partial"))
    not_found = sum(1 for c in heavy if c.tag is not None and c.tag.found == "no")
    if not_found:
        parts.append(f"Claude said it didn't find what it looked for {not_found} times")
    return Item(
        "explore_research", saving, len(heavy), _sources(confirmed > 0, confirmed < saving), _clauses(parts),
        example=EXPLORE_RESEARCH_TOKENSAVE_EXAMPLE if tokensave else "",
        title=EXPLORE_RESEARCH_TOKENSAVE_TITLE if tokensave else "",
        where=EXPLORE_RESEARCH_TOKENSAVE_WHERE if tokensave else "",
        trade_off=EXPLORE_RESEARCH_TOKENSAVE_TRADE_OFF if tokensave else "",
        waste=_by_week((c.week, usd) for c, usd in gaps),
        reported_share=confirmed / saving if saving else 0.0,
    )


def _item_plan_hard(h: Habits) -> Item | None:
    hard = [c for c in h.cycles if c.tag is not None and c.tag.level == "hard"]
    unplanned = [c for c in hard if not c.planned]
    planned = [c for c in hard if c.planned]
    if len(unplanned) < 3:
        return None
    rate_u = sum(c.redone for c in unplanned) / len(unplanned)
    rate_p = sum(c.redone for c in planned) / len(planned) if len(planned) >= 3 else None
    if rate_u <= 0 or (rate_p is not None and rate_p >= rate_u):
        return None
    share = 1 - rate_p / rate_u if rate_p is not None else 0.5
    redos = [(c, share * c.redo_cost) for c in unplanned if c.redone]
    text = f"{len(unplanned)} hard asks went ahead without a plan and {rate_u:.0%} of them were redone"
    if rate_p is not None:
        text += f", against {rate_p:.0%} of the {len(planned)} planned ones"
    parts = [text]
    sources = ("reported", "inferred")
    wanted, rated = _helped_by(h, "plan")
    if wanted >= MIN_FEEDBACK_ANSWERS:
        parts.append(f"you said a plan first would have made {wanted} of {rated} pieces cheaper")
        sources = (*sources, "your feedback")
    return Item(
        "plan_hard", sum(usd for _, usd in redos), len(unplanned), sources, _clauses(parts),
        waste=_by_week((c.week, usd) for c, usd in redos), reported_share=0.5, tagged=True,
    )


def plan_hard_already(h: Habits) -> tuple[int, int] | None:
    """``(planned, hard)`` when ``plan_hard`` has nothing to tell you
    because you already plan hard work: at least :data:`MIN_GROUP` hard
    asks, most of them planned (a plan you wrote in plan mode, one you
    approved, or the build after a plan you approved by typing), and none of
    the ones that weren't was redone. ``None`` otherwise."""
    hard = [c for c in h.cycles if c.tag is not None and c.tag.level == "hard"]
    planned = [c for c in hard if c.planned]
    if len(hard) < MIN_GROUP or 2 * len(planned) <= len(hard):
        return None
    if any(c.redone for c in hard if not c.planned):
        return None
    return len(planned), len(hard)


def _item_skip_plan_easy(h: Habits) -> Item | None:
    easy = [c for c in h.cycles if c.tag is not None and c.tag.level == "easy" and c.planned and c.plan_cost]
    saving = sum(c.plan_cost for c in easy)
    # Two easy asks that went through plan mode are no pattern, and a few
    # cents of planning is not worth a habit.
    if len(easy) < MIN_GROUP or saving < MIN_SAVING:
        return None
    return Item(
        "skip_plan_easy", saving, len(easy), ("reported", "inferred"),
        f"{len(easy)} easy asks went through plan mode first.",
        waste=_by_week((c.week, c.plan_cost) for c in easy), reported_share=0.5, tagged=True,
    )


def _item_skill_early(h: Habits) -> Item | None:
    late = [
        (c, name, before)
        for c in h.cycles
        for name, by_you, replies, before in c.skill_calls
        if not by_you and replies >= LATE_SKILL_TURNS
    ]
    wanted = Counter(
        c.tag.skill_name for c in h.cycles if c.tag is not None and c.tag.skill == "would-help" and c.tag.skill_name
    )
    would_help = sum(1 for c in h.cycles if c.tag is not None and c.tag.skill == "would-help")
    if not late and not would_help:
        return None
    parts = []
    names = Counter(name for _, name, _ in late) + wanted
    if late:
        parts.append(f"Claude reached for a skill after {LATE_SKILL_TURNS} or more replies {len(late)} times")
    if would_help:
        parts.append(f"it said a skill would have helped {would_help} times")
    top = names.most_common(1)[0][0] if names else None
    example = EXAMPLES["skill_early"] if top is None else f"/{top} <what you want>, as your first message for this kind of work."
    if top:
        parts.append(f"most often {top}")
    saving = sum(0.5 * before for _, _, before in late)
    # ``saving`` is built entirely from ``late`` (inferred: Claude reached
    # for a skill only after many replies); ``would_help`` (reported) only
    # adds to ``n`` and the evidence text, so it contributes no dollars --
    # the same disconnect CAP-5 fixes for explore_research (A4).
    return Item(
        "skill_early", saving or None, len(late) + would_help, _sources(bool(would_help), bool(late)),
        _clauses(parts), example=example, waste=_by_week((c.week, 0.5 * before) for c, _, before in late),
        reported_share=0.0,
    )


def _item_skill_unneeded(h: Habits) -> Item | None:
    # P4 leftover: no floor here, explicitly (never a guess) -- a skill's
    # own context footprint (what it added when it loaded) isn't a
    # figure this fact model keeps per call; c.skill_calls only carries
    # the cost *before* the skill loaded (skill_early's own evidence),
    # not the skill's own size, so there is nothing defensible to price
    # a fraction of.
    unneeded = [c for c in h.cycles if c.tag is not None and c.tag.skill == "unneeded" and c.skill_calls]
    if len(unneeded) < 2:
        return None
    names = Counter(name for c in unneeded for name, by_you, _, _ in c.skill_calls if not by_you)
    text = f"Claude said the skill it loaded wasn't needed in {len(unneeded)} asks"
    if names:
        text += " (" + ", ".join(name for name, _ in names.most_common(3)) + ")"
    return Item("skill_unneeded", None, len(unneeded), ("reported",), text + ".")


def _item_short_reports(h: Habits) -> Item | None:
    long = [a for a in h.agents if a.report_tokens > LONG_REPORT_TOKENS and not a.capped]
    gaps = [(a, a.report_carry * (a.report_tokens - SHORT_REPORT_TOKENS) / a.report_tokens) for a in long]
    saving = sum(usd for _, usd in gaps)
    if not long or saving <= 0:
        return None
    with_reports = [a for a in h.agents if a.report_tokens]
    capped = _pct(sum(a.capped for a in with_reports), len(with_reports))
    text = (
        f"{len(long)} agent reports ran over {_k(LONG_REPORT_TOKENS)} tokens, {_k(_mean(a.report_tokens for a in long))} "
        "on average, and stayed in the main context"
    )
    if capped is not None:
        text += f"; {capped:.0f}% of briefs asked for a short one"
    return Item(
        "short_reports", saving, len(long), ("inferred",), text + ".", waste=_by_week((a.week, usd) for a, usd in gaps),
        reported_share=0.0,
    )


def _item_better_briefs(h: Habits) -> Item | None:
    restarted = [a for a in h.agents if a.retry in ("brief", "scope")]
    stuck = [a for a in h.agents if a.result in ("partial", "blocked")]
    thin = [a for a in h.agents if a.brief in ("partial", "vague")]
    if len(restarted) + len(stuck) < 2:
        return None
    parts = []
    if restarted:
        parts.append(f"{len(restarted)} agent runs were started again because of the brief or the task")
    if stuck:
        parts.append(f"{len(stuck)} ended partial or blocked")
    if thin:
        parts.append(f"agents called {len(thin)} briefs partial or vague")
    missing = Counter(w for a in h.agents for w in a.missing if w != "none")
    if missing:
        parts.append("most often missing: " + ", ".join(MISSING_LINES[w][0].lower() for w, _ in missing.most_common(2) if w in MISSING_LINES))
    return Item(
        "better_briefs", sum(a.cost for a in restarted) or None, len(restarted) + len(stuck), ("reported",),
        _clauses(parts), waste=_by_week((a.week, a.cost) for a in restarted),
    )


def _item_flatten_nesting(h: Habits) -> Item | None:
    overlap = [a for a in h.agents if a.overlap_reads]
    nested = [a for a in h.agents if a.depth >= 2]
    saving = sum(a.overlap_cost for a in overlap)
    if not overlap and len(nested) < 3:
        return None
    parts = []
    if overlap:
        parts.append(f"agents read {sum(a.overlap_reads for a in overlap)} files the main session had already read")
    if nested:
        share = _pct(sum(a.cost for a in nested), sum(a.cost for a in h.agents) or 0)
        text = f"{len(nested)} agents were started by other agents"
        if share:
            text += f" ({share:.0f}% of what agents cost)"
        parts.append(text)
    return Item(
        "flatten_nesting", saving or None, sum(a.overlap_reads for a in overlap) + len(nested), ("inferred",),
        _clauses(parts), waste=_by_week((a.week, a.overlap_cost) for a in overlap),
        reported_share=0.0,
    )


def _item_quiet_output(h: Habits) -> Item | None:
    found = []
    for c in h.cycles:
        said = c.tag.out if c.tag is not None else None
        share = {"unneeded": 1.0, "part": 0.5, "needed": 0.0}.get(said, 0.5)
        for tool, tokens, cost in c.big_outputs:
            found.append((c, tool, tokens, share * cost, said is not None))
    saving = sum(f[3] for f in found)
    if not found or saving <= 0:
        return None
    tool = Counter(f[1] for f in found).most_common(1)[0][0]
    reported_saving = sum(f[3] for f in found if f[4])
    return Item(
        "quiet_output", saving, len(found), _sources(any(f[4] for f in found), True),
        f"{len(found)} tool outputs of {_k(BIG_OUTPUT_TOKENS)} tokens or more, mostly from {tool}, stayed in context "
        "for the rest of the work.",
        waste=_by_week((f[0].week, f[3]) for f in found),
        reported_share=reported_saving / saving if saving else 0.0,
    )


def _item_targeted_checks(h: Habits) -> Item | None:
    # CAP-5: derive a fallback for ``check`` -- a test-runner command
    # actually running (``checked_by_tool``, from the same closed prefix
    # set ``classify_purpose`` uses) is evidence a message was checked
    # even without a ``[cg: check=...]`` tag, and stronger evidence than
    # one that contradicts it (said "none" but ran a test anyway).
    reported_checked = [c for c in h.cycles if c.tag is not None and c.tag.check]
    inferred_checked = [c for c in h.cycles if c.checked_by_tool]
    checked = list({id(c): c for c in (*reported_checked, *inferred_checked)}.values())
    if len(checked) < MIN_GROUP:
        return None
    unchecked = [c for c in checked if c.tag is not None and c.tag.check == "none" and not c.checked_by_tool]
    full = [c for c in checked if c.tag is not None and c.tag.check == "full"]
    redone = [c for c in unchecked if c.redone]
    if not redone and len(full) < MIN_GROUP:
        return None
    parts = []
    if unchecked:
        parts.append(f"{len(unchecked)} changes weren't checked and {len(redone)} of them were redone")
    if full:
        parts.append(f"{len(full)} ran the full suite")
    contradicted = sum(1 for c in reported_checked if (c.written or c.tag).check == "none" and c.checked_by_tool)
    if contradicted:
        parts.append(f"{contradicted} said unchecked but a test command ran anyway")
    # Every dollar of saving is earned by an *unchecked, redone* cycle,
    # which needs the tag (there's no deriving "unchecked" from a
    # command's absence) -- the derived signal only widens `n`/evidence,
    # so it stays fully reported.
    return Item(
        "targeted_checks", sum(0.5 * c.redo_cost for c in redone) or None, len(checked),
        _sources(bool(reported_checked), bool(inferred_checked)),
        _clauses(parts), waste=_by_week((c.week, 0.5 * c.redo_cost) for c in redone), tagged=True,
    )


def _item_allow_routine(h: Habits) -> Item | None:
    # SIG-2: h.waits' "permission" count is the same free signal as
    # h.permission_prompts (one row per PermissionRequest hook call, the
    # other per prompt-decision Notification for the same event), so it
    # isn't added to `prompts` again here -- only "idle" (Claude finished
    # a turn and sat waiting for you) rides along, as evidence text: it
    # has no cost of its own (lost time, not spend), so it never enters
    # the saving figure below. A plan to approve or a question to answer
    # (_DIALOG_TOOLS) is a dialog no allow rule would end, so those
    # prompts are left out of the count.
    asked = Counter({tool: n for tool, n in h.permission_prompts.items() if tool not in _DIALOG_TOOLS})
    prompts = sum(asked.values())
    idle = h.waits.get("idle", 0)
    blocked = [c for c in h.cycles if c.blocked]
    count = sum(c.blocked for c in blocked)
    if prompts < 5 and count < 3:
        return None
    parts = []
    if prompts:
        tools = ", ".join(tool for tool, _ in asked.most_common(2))
        parts.append(f"Claude asked for permission {prompts} times, mostly for {tools}")
    if count:
        parts.append(f"auto mode blocked {count} requests and Claude had to find another way")
    if idle:
        parts.append(f"Claude sat waiting for you {idle} times")
    return Item(
        "allow_routine", sum(c.blocked_cost for c in blocked) or None, prompts + count, ("inferred",),
        _clauses(parts), waste=_by_week((c.week, c.blocked_cost) for c in blocked),
        reported_share=0.0,
    )


def _item_state_limits(h: Habits) -> Item | None:
    refused = [c for c in h.cycles if c.refused]
    count = sum(c.refused for c in refused)
    if count < 3:
        return None
    return Item(
        "state_limits", sum(c.refused_cost for c in refused) or None, count, ("inferred",),
        f"{count} requests were turned down, by a deny rule or by you, in {len(refused)} messages, and "
        "Claude had to change course.",
        waste=_by_week((c.week, c.refused_cost) for c in refused),
        reported_share=0.0,
    )


def _effort_waste(c: CycleFact) -> float:
    return 0.5 * c.thinking_cost if c.thinking_cost else 0.25 * c.output_cost


def _thinking_share(cycles) -> float | None:
    """The share of ``cycles``' output that was thinking, in percent."""
    output = sum(c.output_cost for c in cycles)
    return _pct(sum(c.thinking_cost for c in cycles), output) if output else None


def _item_effort_fit(h: Habits) -> Item | None:
    """UX-3: gated the same way as the ``effort-mismatch`` rule this item
    is ``COVERED_BY`` (:data:`_EFFORT_MIN_MESSAGES` messages, more than
    ``h.effort_share_threshold_pct`` of output spent thinking) -- one
    shared effort threshold, not two that can disagree. Three more gates
    keep it to a case where lowering effort for easy work is the fix:
    easy work must think :data:`EFFORT_EASY_OVER_HARD_PTS` points more than
    hard work does at the same effort (when effort is flat, easy work
    isn't wasting any), and the habit must be worth at least
    :data:`MIN_SAVING` a week."""
    easy = [c for c in h.cycles if c.tag is not None and c.tag.level == "easy" and c.effort in _HIGH_EFFORT]
    if len(easy) < _EFFORT_MIN_MESSAGES:
        return None
    share = _thinking_share(easy)
    if share is None or share <= h.effort_share_threshold_pct:
        return None
    hard = [c for c in h.cycles if c.tag is not None and c.tag.level == "hard" and c.effort in _HIGH_EFFORT]
    against = _thinking_share(hard) if len(hard) >= _EFFORT_MIN_MESSAGES else None
    if against is None or share < against + EFFORT_EASY_OVER_HARD_PTS:
        return None
    saving = sum(_effort_waste(c) for c in easy)
    if saving / h.span_weeks < MIN_SAVING:
        return None
    return Item(
        "effort_fit", saving, len(easy), ("reported",),
        f"{len(easy)} easy asks ran at high effort or above, and {share:.0f}% of their output was thinking, "
        f"against {against:.0f}% for hard work.",
        waste=_by_week((c.week, _effort_waste(c)) for c in easy), tagged=True,
    )


def _item_outcome_misses(h: Habits) -> Item | None:
    # P4 leftover: a piece that missed its goal or was stopped still has
    # a known full cost (Piece.cost, your own /cg-feedback rating), so
    # unlike skill_unneeded there is a defensible floor -- half of it,
    # the same conservative fraction split_large/paste_errors/etc. use
    # for a redo or a block tied to a real cost figure, not an invented
    # percentage. The other half is left uncounted: a missed or stopped
    # piece usually still produced some of what you asked for.
    #
    # No ``waste=`` trend: Piece carries no week/timestamp (it's built
    # from a feedback answer, not a cycle), so there is nothing to key a
    # by-week breakdown on.
    misses = [p for p in h.pieces if p.outcome in ("missed", "stopped")]
    met = [p for p in h.pieces if p.outcome == "met"]
    if not misses:
        return None
    saving = 0.5 * sum(p.cost for p in misses)
    parts = [f"{len(misses)} pieces of work missed their goal or were stopped"]
    if met and _mean(p.cost for p in met):
        parts[0] += f", costing {_mean(p.cost for p in misses) / _mean(p.cost for p in met):.1f}x one that met it"
    task = _dominant(p.task for p in misses)
    if task:
        parts.append(f"mostly {catalogue.task_words(task)} work")
    slow = Counter(w for p in misses for w in (*p.slow, *p.why)).most_common(1)
    if slow:
        parts.append(f"slowed most by: {_SLOWED_LABELS.get(slow[0][0], slow[0][0]).lower()}")
    return Item("outcome_misses", saving or None, len(misses), ("your feedback",), _clauses(parts))


#: ``check_work``'s title and where it is put into practice, by where the
#: thing Claude missed was (``missed_in``'s words; ``""`` when you didn't
#: say). The line to paste is :data:`catalogue.MISSED_IN_LINES`.
_CHECK_WORK_VARIANTS = {
    "message": ("Have Claude restate your request as a checklist first", ""),
    "plan": ("Have Claude tick off each plan step before it says done", ""),
    "standing": (
        "Make the rule Claude missed hard to miss",
        "Your CLAUDE.md or memory files, or a hook in settings.json that runs the check.",
    ),
    "earlier": (
        "Restate details that came up earlier, or clear with a handoff",
        "Nowhere in Claude Code's config. This is restating the detail, or /clear after saving a handoff.",
    ),
}
_MISSED_PLACES = {
    "message": "your message",
    "plan": "the plan",
    "standing": "CLAUDE.md or memory",
    "earlier": "earlier in the chat",
}


def _item_check_work(h: Habits) -> Item | None:
    """Follow-ups that fixed something Claude missed although you had said
    it: pieces where you answered that (``why`` has ``missed``), and fixes
    after a plan where the plan check or the plan question says the plan
    already covered it. Those two are missed in the plan, so they join the
    pieces whose ``missed_in`` says the plan."""
    pieces = _answered(h)
    missed = [p for p in pieces if "missed" in p.reasons()]
    in_plan = {id(p) for p in missed if p.missed_in == "plan"}
    covered = [p for p in pieces if p.plan == "covered" and id(p) not in in_plan]
    checks = [c for c in h.cycles if c.plan_check == "covered" and not c.in_missed_piece]
    where = Counter(p.missed_in or "" for p in missed)
    where["plan"] += len(covered) + len(checks)
    n = len(missed) + len(covered) + len(checks)
    if n < MIN_FEEDBACK_ANSWERS:
        return None
    cost = sum(p.followup_cost for p in (*missed, *covered)) + sum(c.cost for c in checks)
    tokens = sum(p.followup_tokens for p in (*missed, *covered)) + sum(c.tokens for c in checks)
    top = where.most_common(1)[0][0]
    parts = [f"{n} times you fixed something Claude missed that your request or plan already said"]
    if top in _MISSED_PLACES:
        parts.append(f"most were in {_MISSED_PLACES[top]}")
    if tokens:
        parts.append(f"fixing them used about {_k(tokens)} tokens")
    title, where_text = _CHECK_WORK_VARIANTS.get(top, ("", ""))
    return Item(
        "check_work", 0.5 * cost or None, n, ("your feedback",), _clauses(parts),
        example=catalogue.MISSED_IN_LINES.get(top, catalogue.MISSED_IN_LINES[""]),
        title=title, where=where_text,
        waste=_by_week((c.week, 0.5 * c.cost) for c in checks),
    )


_BUILDERS = (
    _item_split_large, _item_batch_small, _item_clear_between, _item_brief_clearly, _item_name_files,
    _item_paste_errors, _item_explore_research, _item_plan_hard, _item_skip_plan_easy, _item_skill_early,
    _item_skill_unneeded, _item_short_reports, _item_better_briefs, _item_flatten_nesting, _item_quiet_output,
    _item_targeted_checks, _item_allow_routine, _item_state_limits, _item_effort_fit,
    _item_outcome_misses, _item_check_work,
)


def playbook(h: Habits) -> list[Item]:
    """The habits worth trying, the largest weekly saving first; items
    without an estimate come after, most evidence first."""
    items = [item for item in (build(h) for build in _BUILDERS) if item is not None]
    calibration = _self_report_calibration(h)
    if calibration is not None and calibration["contradicts"]:
        for item in items:
            if item.key in _LEVEL_ITEMS:
                item.evidence += (
                    " Your feedback says work Claude called easy missed its goal more often than normal "
                    "work, so this is low confidence."
                )
    notes = _information_notes(h)
    if notes:
        # The other habits about briefing Claude cite what your answers say
        # too (``brief_clearly`` already did, where it is built).
        for item in items:
            if ITEMS[item.key][0] == "information" and item.key != "brief_clearly":
                item.evidence += " " + _clauses(notes)
    items.sort(key=lambda i: (i.saving is None, -(i.saving or 0.0), -i.n))
    return items


def _asks_per_week(h: Habits, tagged: bool = False) -> Counter:
    """The messages that asked for something, by week: what a weekly rate
    per message divides by (``CycleFact.asks``). With ``tagged``, only those
    Claude tagged: what a habit that is built from tags divides by."""
    per_week: Counter = Counter()
    for c in h.cycles:
        if c.week and (c.tag is not None or not tagged):
            per_week[c.week] += c.asks
    return per_week


def _capture_week(h: Habits) -> str:
    """The Monday of the week capture was turned on (``""`` when that isn't
    known): the weeks before it have no tags to measure."""
    return _week(_moment(h.since), h.tz) if h.since else ""


def _weekly_rates(h: Habits, item: Item) -> list[float | None]:
    """What ``item`` addresses per message in each of the last weeks, ``None``
    for a week that can't be measured: one with too few messages (only the
    tagged ones, for a habit built from tags), or, for a habit whose dollars
    need capture or your feedback, one before capture was turned on."""
    per_week = _asks_per_week(h, item.tagged)
    start = _capture_week(h) if item.reported_share > 0 else ""
    weeks = h.weeks[-TREND_WEEKS:]
    return [
        None if (start and w < start) or per_week[w] < TREND_MIN_CYCLES else item.waste.get(w, 0.0) / per_week[w]
        for w in weeks
    ]


def trend(h: Habits, item: Item) -> tuple[str, str, float]:
    """``(word, weeks, adopted)``: whether what the habit addresses per
    message is falling, rising or steady over the last weeks; each week's
    value scaled to 0-100 (``-`` for a week that can't be measured: too few
    messages, or before capture was turned on); and the weekly saving the
    fall already makes. A message here is one that asked for something, one
    you typed while Claude worked included (``CycleFact.asks``): a go-ahead,
    a status check or a reply to a plan is not one. The word is ``new``
    until :data:`TREND_MIN_WEEKS` weeks are measured, and ``unmeasured`` when
    every measured week is zero: nothing to follow."""
    rates = _weekly_rates(h, item)
    top = max((r for r in rates if r is not None), default=0.0)
    weeks = " ".join("-" if r is None else str(round(100 * r / top)) if top else "0" for r in rates)
    known = [r for r in rates if r is not None]
    if len(known) < TREND_MIN_WEEKS:
        return "new", weeks, 0.0
    if not top:
        return "unmeasured", weeks, 0.0
    half = len(known) // 2
    # Medians, so one unusual week doesn't make or break a trend; and the
    # latest week must not be back above the earlier level.
    before, after = statistics.median(known[:half]), statistics.median(known[-half:])
    if before and after <= 0.8 * before and known[-1] <= before:
        if len(known) < TREND_MIN_ADOPTED:
            return "falling", weeks, 0.0
        per_week = _asks_per_week(h, item.tagged)
        recent = [per_week[w] for w, r in zip(h.weeks[-TREND_WEEKS:], rates) if r is not None][-half:]
        return "falling", weeks, (before - after) * (_mean(recent) or 0.0)
    if after >= 1.2 * before and after > 0:
        return "rising", weeks, 0.0
    return "steady", weeks, 0.0


def confidence(item: Item, *, self_report_ok: bool | None = None) -> str:
    """``self_report_ok`` is ``_self_report_calibration``'s verdict on
    whether the ``level`` Claude reports carries signal (``None`` while
    there isn't enough feedback to tell): ``False`` caps a
    ``_LEVEL_ITEMS`` habit at low confidence, whatever ``item.n`` says,
    because the reports it's built on may not be reliable."""
    if self_report_ok is False and item.key in _LEVEL_ITEMS:
        return "low"
    level = "high" if item.n >= 20 else "medium" if item.n >= 8 else "low"
    if item.sources == ("inferred",) and level == "high":
        return "medium"
    return level


def _ranks(values: list[float]) -> list[float]:
    """1-based ranks, tied values sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _percentile_ranks(values: list[float]) -> list[float]:
    """Each value's rank rescaled to 0-1 (the lowest is 0, the highest is
    1); every value gets 0.5 when there's only one of them to rank."""
    n = len(values)
    if n < 2:
        return [0.5] * n
    return [(r - 1) / (n - 1) for r in _ranks(values)]


def _auc(scores: list[float], positive: list[bool]) -> float | None:
    """CAP-6: the rank-sum (Mann-Whitney) AUC of ``scores`` discriminating
    ``positive`` -- 0.5 is no better than chance, 1.0 perfect, 0.0
    perfectly backwards. ``None`` without at least one of each class."""
    pos_n = sum(positive)
    neg_n = len(positive) - pos_n
    if not pos_n or not neg_n:
        return None
    ranks = _ranks(scores)
    rank_sum_pos = sum(r for r, p in zip(ranks, positive) if p)
    return (rank_sum_pos - pos_n * (pos_n + 1) / 2) / (pos_n * neg_n)


#: CAP-6: reported word -> its ordinal position, low to high, for the AUC
#: helpers below (``d_level``/``brief_clarity_index``): the direction a
#: genuine self-report should point outcomes.
_LEVEL_ORDER = {"easy": 0, "normal": 1, "hard": 2}
_BRIEF_ORDER = {"clear": 0, "partial": 1, "vague": 2}


def _rated_outcomes(h: Habits) -> list[CycleFact]:
    """Cycles whose outcome came from your own answer or rating -- never
    Claude's own ``[cg-fb: ...]`` tag (SEC-P1)."""
    return [c for c in h.cycles if c.outcome and c.outcome_source != "tag"]


def _d_level_of(rated: list[CycleFact]) -> float | None:
    """The ``d_level`` core (``2 * AUC - 1`` for reported ``level``
    discriminating a missed outcome), factored out of :func:`d_level` so
    :func:`d_level_stability` (CAP-7) can run it twice, on two halves of
    the same rated population, without duplicating the AUC/``MIN_GROUP``
    logic or risking the two ever drifting apart. ``None`` under
    ``MIN_GROUP`` messages, same as :func:`d_level`."""
    if len(rated) < MIN_GROUP:
        return None
    auc = _auc([_LEVEL_ORDER[c.tag.level] for c in rated], [c.outcome == "missed" for c in rated])
    return None if auc is None else 2 * auc - 1


def d_level(h: Habits) -> float | None:
    """CAP-6: ``2 * AUC - 1`` for reported ``level`` (easy < normal <
    hard) discriminating your feedback's outcome (``missed`` as the
    positive class). Positive when harder self-reports genuinely track
    more misses (real signal); at or below zero when they don't -- the
    generalisation of ``_self_report_calibration``'s easy-vs-normal check
    across all three words at once. ``None`` under ``MIN_GROUP`` rated
    messages."""
    rated = [c for c in _rated_outcomes(h) if c.tag is not None and c.tag.level in _LEVEL_ORDER]
    return _d_level_of(rated)


#: CAP-7: how far apart the first and second half's ``d_level`` may be
#: and still count as settled (:func:`d_level_stability`).
#: Assumption: a tenth of the full -1..1 range, because that is the
#: same order of magnitude ``_self_report_calibration`` already treats
#: as decisive on its own (whether ``d_level`` is positive or negative
#: at all, not its exact digit), and small enough that two random
#: halves of a genuinely-tracking self-report rarely straddle it by
#: chance once each half already clears ``MIN_GROUP``.
D_LEVEL_STABILITY_TOLERANCE = 0.1


def d_level_stability(h: Habits) -> dict | None:
    """CAP-7: whether ``d_level`` has settled -- the precondition a
    metrics-capture level step-down suggestion checks before it trusts
    the calibration signal it would cite. Splits the rated cycles
    chronologically (oldest first, untimed cycles sorted first as the
    oldest-looking) into two halves and computes :func:`_d_level_of` on
    each independently. ``None`` while either half has fewer than
    ``MIN_GROUP`` rated messages (so at least ``2 * MIN_GROUP`` total is
    needed before this has anything to say), or while either half's AUC
    itself is undefined (all one outcome within that half).

    Assumption: "stable" means the two halves' ``d_level`` are within
    :data:`D_LEVEL_STABILITY_TOLERANCE` of each other -- close enough
    that more evidence looks unlikely to flip the picture, not that the
    two numbers must match exactly."""
    rated = sorted(
        (c for c in _rated_outcomes(h) if c.tag is not None and c.tag.level in _LEVEL_ORDER),
        key=lambda c: c.ts or datetime.min.replace(tzinfo=timezone.utc),
    )
    if len(rated) < 2 * MIN_GROUP:
        return None
    mid = len(rated) // 2
    first, second = _d_level_of(rated[:mid]), _d_level_of(rated[mid:])
    if first is None or second is None:
        return None
    return {"first": first, "second": second, "stable": abs(first - second) < D_LEVEL_STABILITY_TOLERANCE}


def brief_clarity_index(h: Habits) -> float | None:
    """CAP-6: ``d_level``'s twin for reported brief clarity (clear <
    partial < vague) discriminating your feedback's outcome. Positive
    when a vaguer brief genuinely tracks more misses. ``None`` under
    ``MIN_GROUP`` rated messages."""
    rated = [c for c in _rated_outcomes(h) if c.tag is not None and c.tag.brief in _BRIEF_ORDER]
    if len(rated) < MIN_GROUP:
        return None
    auc = _auc([_BRIEF_ORDER[c.tag.brief] for c in rated], [c.outcome == "missed" for c in rated])
    return None if auc is None else 2 * auc - 1


def _effort_percentiles(h: Habits) -> dict[int, float]:
    """CAP-6: each cycle's cost percentile rank among cycles of the same
    reported ``task`` -- the "effort index" gap 4/CAP-6 name, keyed by
    ``id(cycle)`` (only meaningful for the run that built ``h``). A
    cycle with no reported task, or the only one of its task, gets no
    entry (nothing to rank it against)."""
    groups: dict[str, list[CycleFact]] = {}
    for c in h.cycles:
        if c.tag is not None and c.tag.task:
            groups.setdefault(c.tag.task, []).append(c)
    out: dict[int, float] = {}
    for cycles in groups.values():
        if len(cycles) < 2:
            continue
        for c, pct in zip(cycles, _percentile_ranks([c.cost for c in cycles])):
            out[id(c)] = pct
    return out


def contradiction_flags(h: Habits) -> dict[str, int]:
    """CAP-6: concrete per-message contradictions between a self-report
    and what was actually derived or measured, as counts (not a trend
    like ``d_level``): ``checked_contradicted`` said ``check=none`` but a
    test command ran anyway (CAP-5's ``checked_by_tool``); ``easy_high_effort``
    said ``level=easy`` but cost landed in the priciest quarter of
    same-task messages (``EASY_HIGH_EFFORT_PCT``)."""
    checked_contradicted = sum(
        1 for c in h.cycles if c.tag is not None and (c.written or c.tag).check == "none" and c.checked_by_tool
    )
    pcts = _effort_percentiles(h)
    easy_high_effort = sum(
        1 for c in h.cycles
        if c.tag is not None and c.tag.level == "easy" and pcts.get(id(c), 0.0) >= EASY_HIGH_EFFORT_PCT
    )
    return {"checked_contradicted": checked_contradicted, "easy_high_effort": easy_high_effort}


def _self_report_calibration(h: Habits) -> dict | None:
    """Whether the ``level`` word Claude reports carries signal: work it
    called "easy" missing its goal (your feedback, never its own report)
    more often than "normal" work, with at least ``MIN_GROUP`` rated
    messages on each side to compare. ``None`` while there isn't enough
    feedback yet to tell either way. SEC-P1: a cycle rated only by
    Claude's own ``[cg-fb: ...]`` tag doesn't count -- calibration needs
    your answers or your dashboard rating, not Claude grading itself.

    CAP-6 additionally plugs in three richer signals, all consistency
    checks in their own right, not only inputs to ``contradicts``:
    ``d_level`` and ``brief_clarity`` (this file's ``d_level``/
    ``brief_clarity_index``, the AUC-based generalisation of the
    easy-vs-normal check to every level/brief word and to a continuous
    score instead of a single flip), and ``contradiction_flags`` (named,
    concrete per-message contradictions). Any of the three tips
    ``contradicts`` too, at ``MIN_GROUP`` messages' worth of evidence --
    that flows into ``confidence`` already, through ``playbook_table``'s
    existing ``self_report_ok`` plumbing, so ``confidence`` itself needs
    no new parameter."""
    def _rated(word: str) -> list[CycleFact]:
        return [
            c for c in h.cycles
            if c.tag is not None and c.tag.level == word and c.outcome and c.outcome_source != "tag"
        ]

    easy, normal = _rated("easy"), _rated("normal")
    if len(easy) < MIN_GROUP or len(normal) < MIN_GROUP:
        return None
    easy_missed = _pct(sum(c.outcome == "missed" for c in easy), len(easy)) or 0.0
    normal_missed = _pct(sum(c.outcome == "missed" for c in normal), len(normal)) or 0.0
    d = d_level(h)
    brief_clarity = brief_clarity_index(h)
    flags = contradiction_flags(h)
    contradicts = (
        easy_missed > normal_missed
        or (d is not None and d < 0)
        or flags["checked_contradicted"] >= MIN_GROUP
        or flags["easy_high_effort"] >= MIN_GROUP
    )
    return {
        "easy_missed_pct": easy_missed,
        "normal_missed_pct": normal_missed,
        "contradicts": contradicts,
        "d_level": d,
        "brief_clarity": brief_clarity,
        "contradiction_flags": flags,
    }


def _self_report_note(h: Habits) -> str | None:
    calibration = _self_report_calibration(h)
    if calibration is None:
        return None
    easy, normal = calibration["easy_missed_pct"], calibration["normal_missed_pct"]
    if calibration["contradicts"]:
        note = (
            f"Work Claude called easy missed its goal {easy:.0f}% of the time, more often than normal work at "
            f"{normal:.0f}%: treat what it calls easy with caution. Habits built on it (effort, planning) show low "
            "confidence."
        )
    else:
        note = (
            f"Work Claude called easy missed its goal {easy:.0f}% of the time, no more often than normal work at "
            f"{normal:.0f}%: its reports carry signal."
        )
    contradicted = calibration["contradiction_flags"]["checked_contradicted"]
    if contradicted >= MIN_GROUP:
        note += f" {contradicted} times it said a change was unchecked but a test command ran anyway."
    return note


# -- tables --------------------------------------------------------------------


def _money_or_none(usd: float | None) -> float | None:
    return round(usd, 6) if isinstance(usd, (int, float)) and usd > 0 else None


def _picked_up(h: Habits, items: list[Item]) -> list[tuple[Item, float]]:
    """The habits whose trend says you already picked them up, with the
    weekly saving each fall makes."""
    found = [(item, trend(h, item)[2]) for item in items]
    return [(item, usd) for item, usd in found if usd > 0]


def _worth_trying(h: Habits, items: list[Item]) -> list[Item]:
    """``items`` less the habits you already picked up: one is never both
    "worth trying" and "already picked up"."""
    picked = {item.key for item, _ in _picked_up(h, items)}
    return [item for item in items if item.key not in picked]


def playbook_table(h: Habits, items: list[Item]) -> Table:
    items = _worth_trying(h, items)
    weeks = h.span_weeks
    calibration = _self_report_calibration(h)
    self_report_ok = None if calibration is None else not calibration["contradicts"]
    rows = []
    for item in items:
        word, spark, _ = trend(h, item)
        theme, _ = ITEMS[item.key]
        rows.append([
            item.key,
            theme,
            _money_or_none(item.saving / weeks if item.saving else None),
            item.evidence,
            item.example or EXAMPLES[item.key],
            BASES[item.key],
            item.n,
            item.source,
            confidence(item, self_report_ok=self_report_ok),
            word,
            spark,
            # UX-8: where it's put into practice, what trying it costs or
            # risks, and how to go back -- same three-part shape as
            # fixes.py's explainer, added here since a habit has no
            # SettingChange for fixes.build_fixes to work from.
            item.where or WHERE[item.key],
            item.trade_off or TRADE_OFFS[item.key],
            item.undo or UNDO[item.key],
            # UX-3: filled in later by apply_covered_by, once the rules
            # this report actually fired are known -- "" until then, and
            # for any item COVERED_BY doesn't name.
            "",
            # Additive: covered_by_rule, filled in alongside covered_by
            # by the same call.
            "",
            # Additive: the habit's display title, resolved for this
            # report (``item_title``) -- ``ITEMS[item.key][1]``'s fixed
            # one, unless a finding switched it (so far only
            # ``explore_research``, to tokensave's tools). A reader that
            # only knows the old ``ITEMS.get(key, ...)[1]`` lookup (e.g.
            # ``quick_actions._playbook_tips``) still gets a title, just
            # not this report's variant -- it should read this column
            # instead once it has one.
            item_title(item),
        ])
    return Table(
        name="habits_playbook",
        title="Habits worth trying",
        columns=[
            Column(key="habit", label="Habit", kind="str"),
            Column(key="theme", label="Theme", kind="str"),
            Column(key="saving", label="Saving a week", kind="money"),
            Column(key="evidence", label="What your sessions show", kind="str"),
            Column(key="example", label="Try", kind="str"),
            Column(key="basis", label="How the saving is worked out", kind="str"),
            Column(key="n", label="Seen", kind="int"),
            Column(key="source", label="Source", kind="str"),
            Column(key="confidence", label="Confidence", kind="str"),
            Column(key="trend", label="Trend", kind="str"),
            Column(key="weeks", label="By week", kind="str"),
            Column(key="where", label="Where", kind="str"),
            Column(key="trade_off", label="Trade-off", kind="str"),
            Column(key="how_to_undo", label="How to undo it", kind="str"),
            Column(key="covered_by", label="Already covered by", kind="str"),
            #: Additive: the same fact as ``covered_by``, as the
            #: recommendation's own rule id rather than its title, so a
            #: caller (the dashboard) can link straight to it -- ``""``
            #: until ``apply_covered_by`` fills it in, same as
            #: ``covered_by`` itself.
            Column(key="covered_by_rule", label="Covering rule", kind="str"),
            #: Additive: see the append above -- the resolved title,
            #: for a caller to show instead of looking ``habit`` up in
            #: ``ITEMS`` itself.
            Column(key="title", label="Title", kind="str"),
        ],
        rows=rows,
        notes=[] if rows else [
            "Nothing to suggest yet. The more you use Claude Code (and the more metrics capture collects), the "
            "more this finds."
        ],
    )


def apply_covered_by(report: "ReportModel") -> None:
    """UX-3: for each habit in :data:`COVERED_BY` whose rule actually
    fired in ``report.recommendations``, drop that habit's own saving
    and name the rule instead, so the same finding isn't reported as
    two separate savings -- one from the playbook, one from
    Recommendations. Mutates the ``habits_playbook`` table found in
    ``report.sections`` in place; a no-op when that table isn't present
    (a report built with ``include`` leaving the habits section out) or
    has no rows.

    Called from ``report.build_report`` right after ``recommend()``
    runs, since the playbook table itself (``playbook_table`` above) is
    built earlier, before any rule has fired -- there is no report yet
    to check ``COVERED_BY`` against at that point.
    """
    table = next(
        (t for section in report.sections for t in section.tables if t.name == "habits_playbook"),
        None,
    )
    if table is None or not table.rows:
        return
    key_idx = next(i for i, c in enumerate(table.columns) if c.key == "habit")
    saving_idx = next(i for i, c in enumerate(table.columns) if c.key == "saving")
    covered_idx = next(i for i, c in enumerate(table.columns) if c.key == "covered_by")
    covered_rule_idx = next(i for i, c in enumerate(table.columns) if c.key == "covered_by_rule")
    rule_titles = {rec.id: rec.title for rec in report.recommendations}
    for row in table.rows:
        rule_id = COVERED_BY.get(row[key_idx])
        if rule_id is not None and rule_id in rule_titles:
            row[saving_idx] = None
            row[covered_idx] = rule_titles[rule_id]
            row[covered_rule_idx] = rule_id


#: ``ReportMeta.window`` for a window of whole days ("last 1 days" is what
#: ``api._window_label`` writes for one).
_LAST_DAYS = re.compile(r"last (\d+) days?")


def _digest_title(window: str) -> str:
    """"Weekly pace (last 7 days)": the digest's title for the report
    window ``window`` (``ReportMeta.window``). A window of days names its
    own count, whatever span the messages in it cover; "all time" and a
    window that starts at a moment (the last hour, today, since your last
    change) have no count, so they read "all time" and "this window". A
    caller that didn't say which window this is gets the bare name."""
    found = _LAST_DAYS.fullmatch(window)
    if found:
        days = int(found.group(1))
        return f"Weekly pace (last {days} day{'s' if days != 1 else ''})"
    if window == "all time":
        return "Weekly pace (all time)"
    return "Weekly pace (this window)" if window else "Weekly pace"


def digest_table(h: Habits, items: list[Item] | None = None) -> Table:
    """"Weekly pace (last N days)": the three habits worth the most, what
    the habits you already picked up save, and what a piece of work that
    met its goal cost.

    UX-4/7 (F3): titled with the picked window (``h.window``), not a bare
    "This week" that implies a calendar week regardless of window, and not
    the span the messages happen to cover: a 7-day window whose messages
    all fall in 3 days is still "last 7 days" (``_digest_title``).
    ``h.span_weeks`` itself no longer stretches a short span into a fake
    weekly rate (see its docstring), so the figures here are already
    honest; the title says which window they are from."""
    items = playbook(h) if items is None else items
    weeks = h.span_weeks
    rows = []
    for n, item in enumerate([i for i in _worth_trying(h, items) if i.saving][:3], start=1):
        rows.append([f"top_{n}", item_title(item), _money_or_none(item.saving / weeks), item.evidence])
    adopted = _picked_up(h, items)
    if adopted:
        rows.append([
            "adopted", "Habits you already picked up",
            _money_or_none(sum(usd for _, usd in adopted)),
            ", ".join(item_title(item) for item, _ in adopted),
        ])
    already = plan_hard_already(h)
    if already:
        rows.append([
            "plan_hard", "Planning hard work first", "Already doing this",
            f"{already[0]} of {already[1]} hard asks were planned, and none of the others was redone",
        ])
    rated = [p for p in h.pieces if p.outcome]
    met = [p for p in rated if p.outcome == "met"]
    if met:
        rows.append([
            "cost_per_met", "A piece of work that met its goal", _money_or_none(_mean(p.cost for p in met)),
            f"{len(met)} of {len(rated)} pieces you gave feedback on",
        ])
    # The share the Capture banner shows: messages from when capture was
    # turned on, in sessions it reached.
    counted = [c for c in h.cycles if not c.outside_capture]
    tagged = sum(1 for c in counted if c.tag is not None)
    if tagged:
        detail = f"{tagged} of {len(counted)}" + (" since you turned capture on" if h.since else "")
        rows.append(["tagged", "Messages Claude tagged", _pct(tagged, len(counted)), detail])
    return Table(
        name="habits_digest",
        title=_digest_title(h.window),
        columns=[
            Column(key="item", label="Item", kind="str"),
            Column(key="what", label="What", kind="str"),
            Column(key="value", label="Value", kind="str"),
            Column(key="detail", label="Detail", kind="str"),
        ],
        rows=rows,
    )


def _by_task_table(h: Habits) -> Table:
    rows = []
    total = len(h.cycles)
    if total:
        rows.append(_task_row("all", h.cycles, total))
    groups: dict[str, list[CycleFact]] = {}
    for c in h.cycles:
        if c.tag is not None and c.tag.task:
            groups.setdefault(c.tag.task, []).append(c)
    for task, cycles in sorted(groups.items(), key=lambda kv: -sum(c.cost for c in kv[1])):
        rows.append(_task_row(task, cycles, total))
    return Table(
        name="habits_by_task",
        title="Kinds of task",
        columns=[
            Column(key="task", label="Task", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="avg_cost", label="Per message", kind="money"),
            Column(key="main_cost", label="Cost (main session only)", kind="money"),
            Column(key="clear_pct", label="Clear asks", kind="pct"),
            Column(key="large_pct", label="Large asks", kind="pct"),
            Column(key="redo_pct", label="Redone", kind="pct"),
            Column(key="met_pct", label="Met the goal", kind="pct"),
        ],
        rows=rows,
        notes=_excused_notes(h),
    )


def _excused_notes(h: Habits) -> list[str]:
    """A note saying how many messages that looked redone are left out of
    every rework figure because your answers say the next message was a
    change of mind or new to the plan."""
    excused = sum(1 for c in h.cycles if c.excused)
    if not excused:
        return []
    return [
        f"{excused} {'message that looked' if excused == 1 else 'messages that looked'} redone "
        f"{'is' if excused == 1 else 'are'} left out of Redone: you called the next message a change of mind "
        "or new to the plan."
    ]


def _task_row(task: str, cycles: list[CycleFact], total: int) -> list:
    briefs = [c for c in cycles if c.tag is not None and c.tag.brief]
    sizes = [c for c in cycles if c.tag is not None and c.tag.size]
    rated = [c for c in cycles if c.outcome]
    cost = sum(c.cost for c in cycles)
    return [
        task,
        len(cycles),
        _pct(len(cycles), total),
        cost,
        cost / len(cycles),
        # PROF-06/F7: "cost" above is the whole piece of work, subagents
        # included; this is the main session's own share of it alone --
        # what a main-session-only reprice (a model or effort change to
        # the top-level settings) actually covers, so scaling that
        # reprice down to a task's share (profiles.goals._task_share)
        # can use a share computed the same way instead of the total.
        sum(c.main_cost for c in cycles),
        _pct(sum(c.tag.brief == "clear" for c in briefs), len(briefs)),
        _pct(sum(c.tag.size in ("l", "xl") for c in sizes), len(sizes)),
        _pct(sum(c.redone for c in cycles), len(cycles)),
        _pct(sum(c.outcome == "met" for c in rated), len(rated)),
    ]


def _briefs_table(h: Habits) -> Table:
    rows = []
    for word in catalogue.TAG_VOCAB["brief"]:
        cycles = [c for c in h.cycles if c.tag is not None and c.tag.brief == word]
        if not cycles:
            continue
        rated = [c for c in cycles if c.outcome]
        rows.append([
            word,
            len(cycles),
            _mean(c.cost for c in cycles),
            _pct(sum(c.redone for c in cycles), len(cycles)),
            _pct(sum(c.outcome == "met" for c in rated), len(rated)),
            ", ".join(MISSING_LINES[w][0].lower() for w in _top_missing(cycles) if w in MISSING_LINES),
        ])
    return Table(
        name="habits_briefs",
        title="How clear your asks were",
        columns=[
            Column(key="brief", label="Brief", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="avg_cost", label="Per message", kind="money"),
            Column(key="redo_pct", label="Redone", kind="pct"),
            Column(key="met_pct", label="Met the goal", kind="pct"),
            Column(key="missing", label="Most often missing", kind="str"),
        ],
        rows=rows,
        notes=_briefs_notes(h) if rows else [],
    )


def _briefs_notes(h: Habits) -> list[str]:
    """What the plain averages in the briefs table leave out, and what the
    like-for-like comparison behind the ``brief_clearly`` card says, so the
    table and the card agree: the table mixes plan builds, kinds of task and
    levels together, which can make clear asks look dearer."""
    mixed = "The averages above mix in plan builds and every kind of work, so they can read differently."
    found = brief_comparison(h)
    # Your own answers can show the card when the tags can't.
    cardless = " Work habits shows no card for them." if _item_brief_clearly(h) is None else ""
    if found is None:
        return [
            "There are too few clear and unclear asks of one kind of task to compare them like for like."
            + cardless,
            mixed,
        ]
    if found.holds:
        cost = f"the median partial or vague ask cost {found.ratio:.1f}x a clear one" if found.ratio else (
            "partial and vague asks cost more than clear ones"
        )
        return [f"Compared like for like, {cost}: the same kind of task, plan builds left out.", mixed]
    return [
        f"Compared like for like, a partial or vague ask cost more than a clear one in "
        f"{100 * found.probability:.0f}% of the comparisons. That is too few to say.{cardless}",
        mixed,
    ]


def _prompt_flags_table(h: Habits) -> Table:
    rows = []
    for flag in (*PROMPT_FLAGS, "paste"):
        with_flag = [c for c in h.cycles if (c.paste if flag == "paste" else flag in c.flags)]
        without = [c for c in h.cycles if not (c.paste if flag == "paste" else flag in c.flags)]
        rows.append([
            flag,
            len(with_flag),
            _pct(len(with_flag), len(h.cycles)),
            _mean(c.cost for c in with_flag),
            _mean(c.cost for c in without),
            _mean(c.reads for c in with_flag),
            _mean(c.reads for c in without),
        ])
    return Table(
        name="habits_prompt_flags",
        title="What your messages contained",
        columns=[
            Column(key="flag", label="Contained", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="avg_with", label="Per message with it", kind="money"),
            Column(key="avg_without", label="Per message without it", kind="money"),
            Column(key="reads_with", label="Reads and searches with it", kind="float"),
            Column(key="reads_without", label="Reads and searches without it", kind="float"),
        ],
        rows=rows if h.cycles else [],
    )


#: The one place the /cg-brief skill is offered, shown with the brief
#: templates while the ``brief_clearly`` card shows.
BRIEF_OFFER = "The optional /cg-brief skill gives Claude the same checklists: claudeglass capture brief on."


def _templates_table(h: Habits, brief_offer: bool = False) -> Table:
    """``brief_offer``: Work habits shows the ``brief_clearly`` card, so the
    table also offers the /cg-brief skill."""
    rows = []
    groups: dict[str, list[CycleFact]] = {}
    for c in h.cycles:
        if c.tag is not None and c.tag.task:
            groups.setdefault(c.tag.task, []).append(c)
    tasks = [t for t, _ in sorted(groups.items(), key=lambda kv: -len(kv[1]))] or list(_DEFAULT_TEMPLATE_TASKS)
    for task in tasks:
        cycles = groups.get(task, [])
        rows.append(_template_row(task, cycles))
    return Table(
        name="habits_brief_templates",
        title="Brief templates",
        columns=[
            Column(key="task", label="Task", kind="str"),
            Column(key="checklist", label="Checklist", kind="str"),
            Column(key="why", label="Why these", kind="str"),
            Column(key="template", label="Template", kind="str"),
        ],
        rows=rows,
        notes=[BRIEF_OFFER] if brief_offer else [],
    )


def template_lines(task: str, cycles=()) -> tuple[list[str], str]:
    """The checklist keys for ``task`` (your most often missing first,
    then its defaults) and why."""
    # A message whose follow-ups you said were things it left out counts
    # twice, in both the words and the messages they are shared over.
    counts = Counter({w: n for w, n in _missing_counts(cycles).items() if w in MISSING_LINES})
    tagged = sum(2 if c.left_out else 1 for c in cycles if c.tag is not None and c.tag.missing)
    mine = [w for w, n in counts.most_common() if tagged and n / tagged >= 0.2]
    keys = mine + [k for k in DEFAULT_CHECKLISTS.get(task, ("goal", "done")) if k not in mine]
    if mine:
        top = mine[0]
        seen = sum(1 for c in cycles if c.tag is not None and top in c.tag.missing)
        why = f"{MISSING_LINES[top][0]} was missing in {seen} of {len(cycles)} {catalogue.task_words(task)} asks."
        if any(c.left_out and c.tag is not None and top in c.tag.missing for c in cycles):
            why += " You said some follow-ups were things your request left out."
    else:
        why = "A starting point; metrics capture (Standard) fits it to what your asks leave out."
    return keys, why


def _template_row(task: str, cycles) -> list:
    keys, why = template_lines(task, cycles)
    lines = [MISSING_LINES[k] if k in MISSING_LINES else _REPORT_LINE for k in keys]
    return [task, " / ".join(label for label, _ in lines), why, "\n".join(text for _, text in lines)]


def _agents_table(h: Habits) -> Table:
    rows = []
    levels = [c for c in h.cycles if c.tag is not None and c.tag.level]
    if levels:
        cost = sum(c.cost for c in h.cycles)
        rows.append([
            "top-level", len(h.cycles), cost, None, None, None, None, None, None, None, None, None,
            _pct(sum(c.tag.level == "easy" for c in levels), len(levels)),
            _pct(sum(c.tag.level == "hard" for c in levels), len(levels)),
            None, None,
        ])
    groups: dict[str, list[AgentFact]] = {}
    for a in h.agents:
        groups.setdefault(a.agent_type, []).append(a)
    for agent_type, runs in sorted(groups.items(), key=lambda kv: -sum(a.cost for a in kv[1])):
        reports = [a for a in runs if a.report_tokens]
        results = [a for a in runs if a.result]
        leveled = [a for a in runs if a.level]
        edited = [a.calls_before_edit for a in runs if a.calls_before_edit is not None]
        rules = Counter(a.rules for a in runs if a.rules)
        rows.append([
            agent_type,
            len(runs),
            sum(a.cost for a in runs),
            _mean(a.report_tokens for a in reports),
            _pct(sum(a.capped for a in runs), len(runs)),
            _pct(sum(a.result == "done" for a in results), len(results)),
            sum(1 for a in runs if a.retry),
            sum(1 for a in runs if a.retry == "model"),
            _pct(sum(a.probe_calls for a in runs), sum(a.calls for a in runs)),
            statistics.median(edited) if edited else None,
            rules.get("used", 0),
            rules.get("unused", 0),
            _pct(sum(a.level == "easy" for a in leveled), len(leveled)),
            _pct(sum(a.level == "hard" for a in leveled), len(leveled)),
            sum(a.overlap_reads for a in runs),
            sum(1 for a in runs if a.depth >= 2),
        ])
    return Table(
        name="habits_agents",
        title="How agents were used",
        columns=[
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="report_tokens", label="Report", kind="tokens"),
            Column(key="capped_pct", label="Asked for a short report", kind="pct"),
            Column(key="done_pct", label="Finished", kind="pct"),
            Column(key="retried", label="Retried", kind="int"),
            Column(key="retried_model", label="Retried for the model", kind="int"),
            Column(key="probe_pct", label="Single read-only calls", kind="pct"),
            Column(key="before_edit", label="Calls before the first edit", kind="float"),
            Column(key="rules_used", label="Used CLAUDE.md", kind="int"),
            Column(key="rules_unused", label="Didn't use CLAUDE.md", kind="int"),
            Column(key="easy_pct", label="Easy work", kind="pct"),
            Column(key="hard_pct", label="Hard work", kind="pct"),
            Column(key="overlap_reads", label="Files read again", kind="int"),
            Column(key="nested", label="Started by an agent", kind="int"),
        ],
        rows=rows,
    )


def _explore_by_model_table(h: Habits) -> Table:
    """What the Explore agents you started cost, per model. This stands in
    for a live hint about reading files yourself: most of an Explore run's
    cost is the context it re-reads, so the model it runs on matters more
    than how many files it opens. A workflow's agents are left out."""
    groups: dict[str, list[AgentFact]] = {}
    for a in h.agents:
        if a.agent_type == _EXPLORE_AGENT and a.direct:
            groups.setdefault(family(a.model) if a.model else "unknown", []).append(a)
    total = sum(a.cost for runs in groups.values() for a in runs)
    rows = [
        [
            model,
            len(runs),
            sum(a.cost for a in runs),
            _mean(a.cost for a in runs),
            _mean(a.context_tokens for a in runs),
            _pct(sum(a.cost for a in runs), total) if total else None,
        ]
        for model, runs in sorted(groups.items(), key=lambda kv: -sum(a.cost for a in kv[1]))
    ]
    return Table(
        name="habits_explore_by_model",
        title="Explore cost by model",
        columns=[
            Column(key="model", label="Model", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="avg_cost", label="Per run", kind="money"),
            Column(key="avg_context", label="Context read per run", kind="tokens"),
            Column(key="share_pct", label="Share of Explore cost", kind="pct"),
        ],
        rows=rows,
    )


def _rows_as_dicts(table: Table) -> list[dict]:
    keys = [c.key for c in table.columns]
    return [dict(zip(keys, row)) for row in table.rows]


def _model_swap_alt(model_swap, agent_type: str) -> tuple[str, float] | None:
    """``(family, saving_pct)`` for ``agent_type`` from ``model_swap`` (a
    ``model_swap.ModelSwapStats``, or anything shaped like one), when a
    cheaper model clears :data:`CHEAPER_MODEL_MIN_PCT`. ``None`` without
    ``model_swap``, with no row for the agent, or when it's already the
    cheapest tier."""
    stats = (getattr(model_swap, "by_key", None) or {}).get(agent_type)
    verdict = getattr(stats, "tier_verdict", None)
    if verdict is None or verdict.state != "cheaper_available" or not verdict.alt_model:
        return None
    if verdict.saving_pct < CHEAPER_MODEL_MIN_PCT:
        return None
    return family(verdict.alt_model), verdict.saving_pct


def _agents_by_task_table(h: Habits, model_swap=None) -> Table:
    """Per kind of task, how each agent type that answered it did: the
    same signals as ``habits_agents``, split by the task of the message
    that spawned each run, plus the cheaper model the model-swap
    evidence supports for that agent type, when nothing vetoes it --
    the agent's corpus-wide ``unfit_agents`` reason, or this task's own
    slice of its runs being mostly hard or retried for the model
    (``model_gate.row_unfit_reason``, the per-task veto F9/PROF-05 added:
    a task can need a larger model even when the agent isn't unfit
    overall). Beside them, what the runs did: how many of their calls
    were a single read-only probe and how many came before the first
    edit, the same two measures as ``habits_agents``."""
    unfit = unfit_agents(_rows_as_dicts(_agents_table(h)))
    groups: dict[str, dict[str, list[AgentFact]]] = {}
    for a in h.agents:
        if not a.task:
            continue
        groups.setdefault(a.task, {}).setdefault(a.agent_type, []).append(a)
    rows = []
    for task in sorted(groups, key=lambda t: -sum(a.cost for runs in groups[t].values() for a in runs)):
        by_agent = groups[task]
        for agent_type in sorted(by_agent, key=lambda a: -sum(x.cost for x in by_agent[a])):
            runs = by_agent[agent_type]
            results = [a for a in runs if a.result]
            leveled = [a for a in runs if a.level]
            edited = [a.calls_before_edit for a in runs if a.calls_before_edit is not None]
            alt = None
            task_row = {
                "runs": len(runs),
                "retried_model": sum(1 for a in runs if a.retry == "model"),
                "hard_pct": _pct(sum(a.level == "hard" for a in leveled), len(leveled)),
            }
            if agent_type not in unfit and len(runs) >= MIN_GROUP and model_gate.row_unfit_reason(task_row) is None:
                alt = _model_swap_alt(model_swap, agent_type)
            rows.append([
                task,
                agent_type,
                len(runs),
                _mean(a.cost for a in runs),
                _pct(sum(a.result == "done" for a in results), len(results)),
                _pct(sum(a.probe_calls for a in runs), sum(a.calls for a in runs)),
                statistics.median(edited) if edited else None,
                alt[0] if alt else None,
                alt[1] if alt else None,
            ])
    return Table(
        name="habits_agents_by_task",
        title="Agents by kind of task",
        columns=[
            Column(key="task", label="Task", kind="str"),
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
            Column(key="avg_cost", label="Per run", kind="money"),
            Column(key="done_pct", label="Finished", kind="pct"),
            Column(key="probe_pct", label="Single read-only calls", kind="pct"),
            Column(key="before_edit", label="Calls before the first edit", kind="float"),
            Column(key="cheaper_model", label="Cheaper model", kind="str"),
            Column(key="cheaper_saving_pct", label="Cheaper by", kind="pct"),
        ],
        rows=rows,
    )


def _effort_table(h: Habits) -> Table:
    groups: dict[str, list[CycleFact]] = {}
    for c in h.cycles:
        if c.tag is not None and c.tag.level:
            groups.setdefault(f"{c.tag.level}:{c.effort or 'default'}", []).append(c)
    order = {w: n for n, w in enumerate(catalogue.TAG_VOCAB["level"])}
    rows = []
    for key, cycles in sorted(groups.items(), key=lambda kv: (order.get(kv[0].split(":")[0], 9), kv[0])):
        rated = [c for c in cycles if c.outcome]
        output = sum(c.output_cost for c in cycles)
        rows.append([
            key,
            len(cycles),
            _mean(c.cost for c in cycles),
            _pct(sum(c.thinking_cost for c in cycles), output) if output else None,
            _pct(sum(c.redone for c in cycles), len(cycles)),
            _pct(sum(c.outcome == "met" for c in rated), len(rated)),
            sum(_effort_waste(c) for c in cycles) if key.split(":")[0] == "easy" and key.split(":")[1] in _HIGH_EFFORT else None,
        ])
    return Table(
        name="habits_effort_fit",
        title="Effort against how hard the work was",
        columns=[
            Column(key="setup", label="Work and effort", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="avg_cost", label="Per message", kind="money"),
            Column(key="thinking_pct", label="Thinking share of output", kind="pct"),
            Column(key="redo_pct", label="Redone", kind="pct"),
            Column(key="met_pct", label="Met the goal", kind="pct"),
            Column(key="saving", label="Lower effort would save, about", kind="money"),
        ],
        rows=rows,
    )


def family(model_id: str | None) -> str:
    """"claude-haiku-4-5-20251001" -> "haiku"; an unknown model keeps its id."""
    for name in _FAMILIES:
        if name in (model_id or ""):
            return name
    return model_id or "unknown"


def went_well(c: CycleFact) -> bool:
    """Your feedback on the message's work where you gave it, otherwise
    whether the messages after it redid, fixed or corrected it."""
    if c.outcome:
        return c.outcome == "met"
    return not c.redone


#: A model's effort when no turn recorded one at all -- "default" isn't
#: the same effort on every model (F6/V25: "Opus 5.5 defaults to
#: medium"), so grouping every unset-effort cycle under a literal
#: "default" bucket compared setups that weren't actually alike. Only
#: the models this project has a verified default for are resolved;
#: everything else keeps the "default" placeholder.
_DEFAULT_EFFORT_BY_FAMILY = {"opus": "medium"}


def _resolved_effort(model: str, effort: str | None) -> str:
    return effort or _DEFAULT_EFFORT_BY_FAMILY.get(family(model), "default")


def _last_cycle_ids(cycles: list[CycleFact]) -> set[int]:
    """``id(cycle)`` for the chronologically last cycle of each session:
    it has no next message that could have redone, fixed or corrected
    it, so counting it in a went-well/redo rate would credit an outcome
    nothing afterwards confirms (PROF-04). ``h.cycles`` holds one
    session's cycles contiguously and in order (``_session`` appends
    them per session as it processes it), so the last one seen per
    ``session_id`` while walking the full list once is correct."""
    last: dict[str, int] = {}
    for c in cycles:
        last[c.session_id] = id(c)
    return set(last.values())


#: The setup comparison's cost signal, reused as-is from quality.py's
#: Signal/compare_runs machinery (PROF-04): ``Signal`` only calls
#: ``num``/``den`` on whatever it's handed, so a ``CycleFact`` works
#: exactly like the ``Run`` quality.py itself compares setups over.
_SETUP_COST_SIGNAL = quality.Signal(
    "cost", "Cost per message", lambda c: c.cost, lambda c: 1.0, "per_run", None, "all", "messages", "money",
)


def _not_ok_signal(last_ids: set[int]) -> quality.Signal:
    """"Didn't go well" (a rise is worse, matching quality.py's own
    failure-rate convention), with each session's last message left out
    of both sides of the ratio (see :func:`_last_cycle_ids`)."""
    return quality.Signal(
        "not_ok", "Messages that didn't go well",
        lambda c: 0.0 if id(c) in last_ids else float(not went_well(c)),
        lambda c: 0.0 if id(c) in last_ids else 1.0,
        "pct", "higher", "all", "messages",
    )


def _setups_table(h: Habits) -> Table:
    """Per kind of task Claude reported, all levels together and then by
    how hard it said the work was: each model, effort and speed setup
    that answered it (split, not collapsed to a model family or a bare
    "default" effort -- F6), and the cheapest that a ratio test with
    Holm correction (``quality.compare_runs``, PROF-04) found no worse
    than your usual one. Shown from :data:`MIN_GROUP` messages; the
    tasks goal only ticks a "cheaper" setup once it also clears
    :data:`TICK_MIN_GROUP` (``profiles.goals._tasks``)."""
    last_ids = _last_cycle_ids(h.cycles)
    groups: dict[str, dict[str, dict[tuple[str, str, str], list[CycleFact]]]] = {}
    for c in h.cycles:
        if c.tag is None or not c.tag.task:
            continue
        setup = (c.model, _resolved_effort(c.model, c.effort), c.speed or "standard")
        by_level = groups.setdefault(c.tag.task, {})
        by_level.setdefault("all", {}).setdefault(setup, []).append(c)
        if c.tag.level:
            by_level.setdefault(c.tag.level, {}).setdefault(setup, []).append(c)
    order = {w: n for n, w in enumerate(("all", *catalogue.TAG_VOCAB["level"]))}
    rows = []
    for task, by_level in sorted(groups.items(), key=lambda kv: (-sum(map(len, kv[1]["all"].values())), kv[0])):
        for level in sorted(by_level, key=lambda word: order.get(word, 9)):
            rows.extend(_setup_rows(task, level, by_level[level], last_ids))
    return Table(
        name="habits_setups",
        title="Best setup for each kind of task",
        columns=[
            Column(key="task", label="Task", kind="str"),
            Column(key="level", label="How hard", kind="str"),
            Column(key="model", label="Model", kind="str"),
            Column(key="effort", label="Effort", kind="str"),
            Column(key="speed", label="Speed", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="avg_cost", label="Per message", kind="money"),
            Column(key="main_avg_cost", label="Per message (main session only)", kind="money"),
            Column(key="ok_pct", label="Went well", kind="pct"),
            Column(key="rated", label="With your feedback", kind="int"),
            Column(key="verdict", label="Setup", kind="str"),
            Column(key="saving_pct", label="Cheaper by", kind="pct"),
        ],
        rows=rows,
    )


def _setup_rows(
    task: str, level: str, setups: dict[tuple[str, str, str], list[CycleFact]], last_ids: set[int]
) -> list[list]:
    not_ok = _not_ok_signal(last_ids)
    stats = []
    for (model, effort, speed), cycles in setups.items():
        rated = [c for c in cycles if id(c) not in last_ids]
        stats.append({
            "model": model,
            "effort": effort,
            "speed": speed,
            "cycles": cycles,
            "n": len(cycles),
            "avg": _mean(c.cost for c in cycles) or 0.0,
            "main_avg": _mean(c.main_cost for c in cycles) or 0.0,
            "ok": _pct(sum(went_well(c) for c in rated), len(rated)) or 0.0,
            "rated": sum(1 for c in cycles if c.outcome),
            "hard_pct": _pct(sum(1 for c in cycles if c.tag is not None and c.tag.level == "hard"), len(cycles)),
        })
    stats.sort(key=lambda s: (-s["n"], s["avg"], s["model"], s["effort"], s["speed"]))
    usual = stats[0]
    best, best_ratio = None, 1.0
    if usual["n"] >= MIN_GROUP and usual["avg"] > 0:
        for s in stats[1:]:
            ratio = s["avg"] / usual["avg"]
            if s["n"] < MIN_GROUP or ratio >= best_ratio:
                continue
            if level == "all" and model_gate.row_unfit_reason({"runs": s["n"], "hard_pct": s["hard_pct"]}):
                # The hard-work veto: mostly hard work under this setup,
                # at the mixed "all" level -- it looks cheap because of
                # what it was used for, not because it's a cheaper
                # setup. The per-level rows below still show it plainly.
                continue
            comparison = quality.compare_runs(usual["cycles"], s["cycles"], [_SETUP_COST_SIGNAL, not_ok])
            if quality.setup_verdict(comparison) in ("worse", "possibly_worse", "mixed"):
                continue
            best, best_ratio = s, ratio
    rows = []
    for s in stats:
        verdict = "usual" if s is usual else "cheaper" if s is best else ""
        saving = 100.0 * (1.0 - best_ratio) if s is best else None
        rows.append([task, level, s["model"], s["effort"], s["speed"], s["n"], s["avg"], s["main_avg"], s["ok"],
                     s["rated"], verdict, saving])
    return rows


def _outcomes_table(h: Habits) -> Table:
    rows = []
    for word in catalogue.FEEDBACK_VOCAB["outcome"]:
        pieces = [p for p in h.pieces if p.outcome == word]
        if not pieces:
            continue
        slow = Counter(w for p in pieces for w in (*p.slow, *p.why)).most_common(1)
        helped = Counter(w for p in pieces for w in p.helped).most_common(1)
        rows.append([
            word,
            len(pieces),
            sum(p.cycles for p in pieces),
            sum(p.cost for p in pieces),
            _mean(p.cost for p in pieces),
            _dominant(p.task for p in pieces) or "",
            _SLOWED_LABELS.get(slow[0][0], slow[0][0]) if slow else "",
            _ANSWER_LABELS["helped"].get(helped[0][0], helped[0][0]) if helped else "",
            " + ".join(sorted({p.source for p in pieces})),
        ])
    return Table(
        name="habits_outcomes",
        title="Did the work meet its goal?",
        columns=[
            Column(key="outcome", label="Outcome", kind="str"),
            Column(key="pieces", label="Pieces of work", kind="int"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="avg_cost", label="Per piece", kind="money"),
            Column(key="task", label="Most often", kind="str"),
            Column(key="slow", label="Slowed most by", kind="str"),
            Column(key="helped", label="Would have helped most", kind="str"),
            Column(key="source", label="Source", kind="str"),
        ],
        rows=rows,
    )


#: ``habits_by_shape`` rows, in order.
SHAPE_LABELS = {
    "plan_build": "Planned and built in one session",
    "plan_only": "Planned, then built elsewhere",
    "no_plan": "No plan",
}


def _by_shape_table(h: Habits) -> Table:
    """Main sessions by whether you planned and built in them, and what
    your feedback said about each kind: whether the work met its goal,
    was worth the tokens, and could have been built from the plan
    alone."""
    rows = []
    total = len(h.shapes)
    for shape in SHAPE_LABELS:
        sessions = [s for s in h.shapes if s.shape == shape]
        if not sessions:
            continue
        work = [p for p in h.pieces if p.shape == shape]
        pieces = [p for p in work if p.outcome]
        worth = [p for p in pieces if p.worth]
        fixes = fixes_after_plan(h, shape)
        carried = [s.carried for s in sessions if s.carried is not None]
        handoff = Counter(p.handoff for p in pieces if p.handoff)
        # Your answer to the plan question, and the plan check's: one of
        # them is asked for a piece, never both.
        plan = Counter(p.plan for p in pieces if p.plan)
        for word in _PLAN_ANSWERS:
            plan[word] += h.plan_checks[(shape, word)]
        rows.append([
            shape,
            len(sessions),
            _pct(len(sessions), total),
            _mean(s.cost for s in sessions),
            round(statistics.median(carried)) if carried else None,
            len(pieces),
            _pct(sum(p.outcome == "met" for p in pieces), len(pieces)),
            _pct(sum(p.worth == "yes" for p in worth), len(worth)),
            _pct(sum(p.worth == "no" for p in worth), len(worth)),
            handoff["yes"],
            handoff["partly"],
            handoff["no"],
            plan["covered"],
            plan["gap"],
            plan["new"],
            len(work),
            fixes.plans if fixes else None,
            fixes.fixed if fixes else None,
            fixes.fixes if fixes else None,
        ])
    return Table(
        name="habits_by_shape",
        title="Planning and building in one session",
        columns=[
            Column(key="shape", label="Session", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="avg_cost", label="Per session", kind="money"),
            Column(key="carried_median", label="Planning kept (median)", kind="int"),
            Column(key="pieces", label="Pieces rated", kind="int"),
            Column(key="met_pct", label="Met the goal", kind="pct"),
            Column(key="worth_pct", label="Worth it", kind="pct"),
            Column(key="costly_pct", label="Too costly", kind="pct"),
            Column(key="handoff_yes", label="Plan was enough", kind="int"),
            Column(key="handoff_partly", label="Plan was partly enough", kind="int"),
            Column(key="handoff_no", label="Needed the discussion", kind="int"),
            Column(key="plan_covered", label="Fix was in the plan", kind="int"),
            Column(key="plan_gap", label="Plan missed it", kind="int"),
            Column(key="plan_new", label="Fix was new", kind="int"),
            Column(key="work_pieces", label="Pieces of work", kind="int"),
            Column(key="plans_built", label="Plans approved", kind="int"),
            Column(key="plans_fixed", label="Plans fixed three times or more", kind="int"),
            Column(key="plan_fixes", label="Fixes after a plan", kind="int"),
        ],
        rows=rows,
    )


def _self_report_row(kind: str, word: str, cycles: list[CycleFact]) -> list | None:
    if not cycles:
        return None
    rated = [c for c in cycles if c.outcome]
    return [
        f"{kind}:{word}",
        len(cycles),
        len(rated),
        _pct(sum(c.outcome == "met" for c in rated), len(rated)),
        _pct(sum(c.outcome == "missed" for c in rated), len(rated)),
        _pct(sum(c.redone for c in cycles), len(cycles)),
    ]


def _self_report_table(h: Habits) -> Table:
    """Claude's own reports against your feedback: for each ``level`` and
    ``brief`` word it tagged a message with, how many messages your
    feedback covers, the share that met or missed its goal, and the
    share the messages after it redid -- so a report that doesn't hold up
    against what you actually said shows up here, not just as a hunch."""
    rows = []
    for word in catalogue.TAG_VOCAB["level"]:
        row = _self_report_row("level", word, [c for c in h.cycles if c.tag is not None and c.tag.level == word])
        if row:
            rows.append(row)
    for word in catalogue.TAG_VOCAB["brief"]:
        row = _self_report_row("brief", word, [c for c in h.cycles if c.tag is not None and c.tag.brief == word])
        if row:
            rows.append(row)
    note = _self_report_note(h)
    return Table(
        name="habits_self_report",
        title="Claude's reports against your feedback",
        columns=[
            Column(key="signal", label="What Claude reported", kind="str"),
            Column(key="cycles", label="Messages", kind="int"),
            Column(key="rated", label="With your feedback", kind="int"),
            Column(key="met_pct", label="Met the goal", kind="pct"),
            Column(key="missed_pct", label="Missed", kind="pct"),
            Column(key="redone_pct", label="Redone afterwards", kind="pct"),
        ],
        rows=rows,
        notes=[note] if note else [],
    )


def _skills_table(h: Habits) -> Table:
    stats: dict[str, dict] = {}

    def entry(name):
        return stats.setdefault(name, {"by_you": 0, "by_claude": 0, "late": 0, "before": [], "helped": 0,
                                       "unneeded": 0, "would_help": 0})

    for c in h.cycles:
        for name, by_you, replies, before in c.skill_calls:
            if by_you:
                continue
            e = entry(name)
            e["by_claude"] += 1
            if replies >= LATE_SKILL_TURNS:
                e["late"] += 1
                e["before"].append(before)
        tag = c.tag
        if tag is not None and tag.skill in ("helped", "unneeded"):
            for name in {n for n, _, _, _ in c.skill_calls}:
                entry(name)[tag.skill] += 1
        if tag is not None and tag.skill == "would-help" and tag.skill_name:
            entry(tag.skill_name)["would_help"] += 1
    for name in h.skill_names | set(stats):
        if name in h.commands_run:
            entry(name)["by_you"] = h.commands_run[name]
    rows = [
        [name, e["by_you"], e["by_claude"], e["late"], _mean(e["before"]), e["helped"], e["unneeded"], e["would_help"]]
        for name, e in sorted(stats.items(), key=lambda kv: -(kv[1]["by_you"] + kv[1]["by_claude"] + kv[1]["would_help"]))
        if name not in catalogue.FEEDBACK_SKILL_NAMES
    ]
    return Table(
        name="habits_skills",
        title="When skills ran",
        columns=[
            Column(key="skill", label="Skill", kind="str"),
            Column(key="by_you", label="You ran it", kind="int"),
            Column(key="by_claude", label="Claude loaded it", kind="int"),
            Column(key="late", label="Loaded late", kind="int"),
            Column(key="before", label="Spent before it, typical", kind="money"),
            Column(key="helped", label="Helped", kind="int"),
            Column(key="unneeded", label="Wasn't needed", kind="int"),
            Column(key="would_help", label="Would have helped", kind="int"),
        ],
        rows=rows,
    )


def _tool_output_table(h: Habits) -> Table:
    stats: dict[str, list] = {}
    for c in h.cycles:
        for tool, tokens, cost in c.big_outputs:
            s = stats.setdefault(tool, [0, 0, 0.0])
            s[0] += 1
            s[1] += tokens
            s[2] += cost
    rows = [[tool, n, tokens, cost] for tool, (n, tokens, cost) in sorted(stats.items(), key=lambda kv: -kv[1][2])]
    return Table(
        name="habits_tool_output",
        title="Big tool output",
        columns=[
            Column(key="tool", label="Tool", kind="str"),
            Column(key="outputs", label="Big outputs", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label="Carrying them cost", kind="money"),
        ],
        rows=rows,
    )


def build_section(
    corpus,
    pricing: Pricing | None,
    *,
    ratings: dict | None = None,
    signals: dict | None = None,
    model_swap=None,
    effort_share_threshold_pct: float = 30.0,
    tz: str | tzinfo | None = None,
    window: str = "",
    since: str = "",
) -> Section:
    """The "habits" report section. Every table is always there, empty
    when there's nothing to show, so the report keeps its shape.
    ``model_swap`` (``model_swap.ModelSwapStats``, already computed for
    the report's own ``model_swap`` section) feeds ``habits_agents_by_task``'s
    cheaper-model column. ``effort_share_threshold_pct`` should be the
    caller's resolved ``recommend.RecommendThresholds
    .effort_mismatch_thinking_share_pct`` (UX-3's shared effort
    threshold), so ``effort_fit`` and the ``effort-mismatch`` rule agree
    on when there's enough to say something. ``tz``, ``window`` and
    ``since`` are ``collect``'s: the zone weeks are counted in, the window
    label the digest is titled with and when capture was turned on."""
    h = collect(
        corpus, pricing, ratings=ratings, signals=signals, effort_share_threshold_pct=effort_share_threshold_pct,
        tz=tz, window=window, since=since,
    )
    return section_from(h, model_swap=model_swap)


def section_from(h: Habits, *, model_swap=None) -> Section:
    items = playbook(h)
    notes = []
    if h.cycles and not any(c.tag is not None for c in h.cycles):
        notes.append(
            "Nothing here was reported by Claude yet. Turn on metrics capture ({{page:setup/capture}} or "
            "claudeglass capture on) to see kinds of task, brief quality and how hard the work was."
        )
    if h.cycles and not any(p.outcome for p in h.pieces):
        notes.append(
            "No feedback yet: rate sessions on {{page:spend/sessions}} or run /cg-feedback to see cost per piece of "
            "work that met its goal."
        )
    return Section(
        key="habits",
        title="Work habits",
        tables=[
            digest_table(h, items),
            playbook_table(h, items),
            _by_task_table(h),
            _briefs_table(h),
            _templates_table(h, brief_card_shown(h, items)),
            _agents_table(h),
            _explore_by_model_table(h),
            _effort_table(h),
            _setups_table(h),
            _agents_by_task_table(h, model_swap),
            _outcomes_table(h),
            _by_shape_table(h),
            _self_report_table(h),
            _prompt_flags_table(h),
            _skills_table(h),
            _tool_output_table(h),
        ],
        notes=notes,
    )


def unfit_agents(rows: list[dict], *, min_sessions: int | None = None) -> dict[str, str]:
    """Agents a cheaper model shouldn't be suggested for, from the
    ``habits_agents`` rows, with why: most of the work was hard, or a run
    was retried for the model. The main session (``top-level``) counts by
    how hard its work was only. The per-row reason logic is shared with
    the model-swap veto/gate helper (``model_gate.row_unfit_reason``),
    which every other "don't suggest this model" check now goes through
    too (F9); ``min_sessions``, when given, is an extra floor on
    ``runs``."""
    out: dict[str, str] = {}
    for row in rows:
        agent = row.get("agent_type")
        if not agent:
            continue
        reason = model_gate.row_unfit_reason(row, min_sessions=min_sessions)
        if reason:
            out[agent] = reason
    return out


# -- the capture section -------------------------------------------------------


#: CAP-3: playbook ``Item.key`` -> ``recommend.py`` ``Recommendation.id`` for
#: pairs that price the SAME underlying waste (finding F4) -- when both a
#: playbook item and a recommendation claim it, ``capture_value_breakdown``
#: takes the larger of the two figures, never their sum.
_CAPTURE_DEDUP_KEYS: dict[str, str] = {
    "effort_fit": "effort-mismatch",
    "short_reports": "agent-report-size",
    "explore_research": "discovery-share",
    "clear_between": "discovery-share",
}


def _recommend_key(r: Recommendation) -> tuple:
    return (r.id, r.agent_type, r.lever)


def capture_recommend_delta(
    with_habits: list[Recommendation], without_habits: list[Recommendation],
) -> tuple[dict[tuple, float], list[Recommendation]]:
    """EST-P7: what ``recommend()`` finds with the habits section present
    against the same run without it, matched by ``(id, agent_type,
    lever)``. Returns the USD each recommendation gains -- it appears, or
    its ``saving_usd`` grows -- keyed the same way, and the recommendations
    ``without_habits`` has that ``with_habits`` doesn't: vetoes, where
    capture's evidence argued a recommendation away rather than for it, so
    they're held back, never counted as a saving."""
    without_by_key = {_recommend_key(r): r for r in without_habits}
    with_by_key = {_recommend_key(r): r for r in with_habits}
    grown: dict[tuple, float] = {}
    for key, r in with_by_key.items():
        before = without_by_key.get(key)
        before_usd = (before.saving_usd or 0.0) if before is not None else 0.0
        after_usd = r.saving_usd or 0.0
        if after_usd > before_usd:
            grown[key] = after_usd - before_usd
    vetoed = [r for key, r in without_by_key.items() if key not in with_by_key]
    return grown, vetoed


def capture_value_breakdown(
    items: list[Item], with_habits: list[Recommendation], without_habits: list[Recommendation],
) -> dict:
    """CAP-3: total USD (over the whole span, not weekly) that metrics
    capture and your feedback are worth -- playbook items weighted by
    ``reported_share`` (A3), plus what capture grows or unlocks in
    ``recommend()`` (EST-P7), deduplicated through ``_CAPTURE_DEDUP_KEYS``
    by taking the larger of the two figures for the same waste, never the
    sum. ``held_back`` is how many recommendations capture's evidence
    argued away (``capture_recommend_delta``'s vetoes)."""
    grown, vetoed = capture_recommend_delta(with_habits, without_habits)
    grown_by_id: dict[str, float] = {}
    for (rec_id, _agent_type, _lever), usd in grown.items():
        grown_by_id[rec_id] = grown_by_id.get(rec_id, 0.0) + usd

    claimed_ids: set[str] = set()
    total = 0.0
    for item in items:
        if not item.saving or item.reported_share <= 0:
            continue
        weighted = item.saving * item.reported_share
        rec_id = _CAPTURE_DEDUP_KEYS.get(item.key)
        if rec_id is not None and rec_id in grown_by_id:
            claimed_ids.add(rec_id)
            total += max(weighted, grown_by_id[rec_id])
        else:
            total += weighted

    # What capture unlocks in recommend() that no playbook item already
    # claims through the dedup map -- counted in full: the with/without
    # diff is itself the evidence, not any one item's reported_share.
    for rec_id, usd in grown_by_id.items():
        if rec_id not in claimed_ids:
            total += usd

    return {"usd": total, "held_back": len(vetoed)}


def capture_dependent_value(
    h: Habits,
    items: list[Item] | None = None,
    *,
    with_habits: list[Recommendation] | None = None,
    without_habits: list[Recommendation] | None = None,
    since: str = "",
) -> float | None:
    """What metrics capture is buying you a week: the habits worth trying
    whose evidence needs it or your feedback, weighted by how genuinely
    reported each item's saving is (``reported_share``, A3) rather than
    gated all-or-nothing, plus what it grows or unlocks in ``recommend()``
    when ``with_habits``/``without_habits`` are given (EST-P7 + CAP-3).
    Spread over the weeks since ``since`` (when capture was enabled), or
    ``h``'s whole span when that isn't known. ``None`` when nothing
    measured yet depends on either."""
    items = playbook(h) if items is None else items
    if with_habits is not None and without_habits is not None:
        total = capture_value_breakdown(items, with_habits, without_habits)["usd"]
    else:
        total = sum(i.saving * i.reported_share for i in items if i.saving and i.reported_share > 0)
    if total <= 0:
        return None
    weeks = capture_mod.weeks_since(since) if since else 0.0
    return total / (weeks or h.span_weeks)


#: CAP-7: the level immediately below each -- only among the levels
#: that ask Claude anything at all (``essentials``, ``standard``,
#: ``deep``; see ``capture_catalogue.LEVEL_GROUPS``). Assumption:
#: stepping ``essentials`` down would land on ``free``, which asks
#: Claude nothing (``capture_catalogue.LEVEL_SUMMARIES``) -- a bigger,
#: different decision than trimming one level's worth of evidence, and
#: already covered by ``capture off`` and by switching a metric off one
#: at a time (the existing "Enough collected for every metric" note in
#: ``capture_view._banner``), so a CAP-7 suggestion never proposes it.
CAPTURE_STEP_DOWN = {"deep": "standard", "standard": "essentials"}


def metric_list(metric_ids) -> str:
    """Capture metrics by their Capture page names, in lower case, for a
    sentence: at most three named, the rest counted ("size of the work,
    planning, skills and 5 more")."""
    names = [
        catalogue.METRICS_BY_ID[i].title.lower() if i in catalogue.METRICS_BY_ID else str(i) for i in metric_ids
    ]
    if len(names) > 3:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def step_down_terms(current: str, target: str) -> str:
    """CAP-7's what/where/trade-off/undo sentence, shared by the report's
    capture-section note and the Capture page's hint
    (``capture_view._step_down_note``): ``capture level`` writes
    ``[capture] level`` and re-syncs the capture hook entries in Claude
    Code's ``settings.json`` (``cli._cmd_capture``), and ``--dry-run``
    shows that diff without writing."""
    return (
        "Stepping down stops collecting them; what they feed keeps what's been gathered but gets no new answers. "
        "It changes [capture] level in ClaudeGlass's config.toml, and Claude Code's settings.json only where "
        f"{catalogue.LEVEL_TITLES[target]} needs fewer hook entries. "
        f"'claudeglass capture level {target} --dry-run' shows what stepping down would change and writes "
        f"nothing; 'claudeglass capture level {current}' undoes it."
    )


def capture_step_down_suggestion(
    h: Habits, capture_config, use: capture_mod.CaptureUsage | None
) -> dict | None:
    """CAP-7: suggest-only (ClaudeGlass never lowers the level itself --
    "no apply button" holds here too) hint that stepping the
    ``[capture] level`` down one step looks safe: every metric that step
    would drop has enough of its own evidence to trust dropping it,
    *and* the self-report signal that evidence backs has stopped moving.
    ``None`` unless both hold, or there's nothing to check against yet
    (``use`` is ``None``, the level isn't in :data:`CAPTURE_STEP_DOWN`,
    or the step would drop nothing Claude is asked for).

    Ready: every dropped metric that asks Claude anything
    (``capture_catalogue.asks_claude``) has at least
    ``capture.enough_target`` answers -- the same per-metric bar
    ``capture_view``'s own "Enough collected" banner note already uses
    (CAP-5/gap 4), just checked against the specific metrics a step down
    would actually drop rather than every metric that's on.

    Stable: :func:`d_level_stability` says so. A step down is never
    suggested from readiness alone -- only once ``d_level`` has settled,
    so the suggestion isn't chasing a number still swinging with each
    new rated message.

    The token sizes are the static per-occurrence figures
    (``capture_catalogue.rough_tokens``, the same ones ``docs/capture.md``
    shows). The dollar saving is each dropped metric's own *measured*
    cost since capture started (``use.by_metric``, the figure
    ``capture_view._worth`` already shows per metric) divided by the
    weeks since -- measured from your own transcripts, not re-derived
    from the static token sizes and a separately modeled request rate.
    ``None`` while there's no start time to spread it over."""
    level = getattr(capture_config, "level", "off") or "off"
    target = CAPTURE_STEP_DOWN.get(level)
    if target is None or use is None:
        return None
    dropped = tuple(
        i for i in catalogue.level_metrics(level)
        if i not in catalogue.level_metrics(target) and catalogue.asks_claude(i)
    )
    if not dropped:
        return None
    if any(use.answers.get(i, 0) < capture_mod.enough_target(i) for i in dropped):
        return None
    stability = d_level_stability(h)
    if stability is None or not stability["stable"]:
        return None
    since = getattr(capture_config, "enabled_at", "") or ""
    weeks = capture_mod.weeks_since(since) if since else None
    measured = sum(use.by_metric.get(i, 0.0) for i in dropped)
    current_sizes = catalogue.rough_tokens(catalogue.level_metrics(level))
    target_sizes = catalogue.rough_tokens(catalogue.level_metrics(target))
    return {
        "current": level,
        "target": target,
        "dropped_metrics": len(dropped),
        "session_note_tokens_saved": max(0, current_sizes["session_note"] - target_sizes["session_note"]),
        "subagent_note_tokens_saved": max(0, current_sizes["subagent_note"] - target_sizes["subagent_note"]),
        "weekly_usd_saved": (measured / weeks) if weeks else None,
        "command": f"claudeglass capture level {target} --dry-run",
        "undo_command": f"claudeglass capture level {level}",
    }


def capture_section(
    corpus,
    pricing: Pricing | None,
    capture_config,
    *,
    ratings: dict | None = None,
    h: Habits | None = None,
    tz: str | tzinfo | None = None,
) -> Section:
    """The "capture" report section: what metrics capture cost while it
    was on, measured from the transcripts (``capture.usage``), a week
    (``capture.weekly_cost``), and what the habits that depend on it or
    your feedback are worth a week (``capture_dependent_value``).

    ``h``, when given, is a :func:`collect` result the caller already
    built for the "habits" section (perf, S5/ROB-P3: avoids a second
    ``collect`` pass over the same corpus). It's only reused as-is when
    its ``effort_share_threshold_pct`` is this function's own default
    (30.0) -- ``build_section``'s caller may resolve that threshold from
    config (UX-3), which this section has never done, so reusing a
    differently-thresholded ``h`` could change ``capture_dependent_value``
    for a config that overrides it. Otherwise a fresh, unthresholded
    ``collect`` runs exactly as before. ``tz`` (``config.tz``) is the zone
    that fresh ``collect`` counts weeks and days in, as the habits
    section's own pass does.

    ``sessions_with_notes`` is ``capture.usage``'s own main-session count
    (SURV-8's gate on the Capture page's per-metric worth table lives
    there, in ``capture_view._worth``); ``after_compact_notes``/
    ``after_compact_cost`` (SURV-3) are the notes that landed after a
    compact boundary, priced at the fuller post-compaction rate, shown as
    their own line rather than folded into a scope's cost. The
    EST-P7/CAP-3 recommend-diff value isn't available yet here --
    ``report.build_report`` patches ``habit_value``/``held_back`` in once
    it has run ``recommend()`` with and without the habits section.

    CAP-7's ``step_down_target``/``step_down_tokens_saved``/
    ``step_down_weekly_saving`` rows carry
    :func:`capture_step_down_suggestion`'s numbers (blank/zero when it
    has nothing to suggest); a note spells it out, in numbers and level
    names only, once there is one -- a suggestion, never applied here or
    anywhere else (no apply button: ClaudeGlass never lowers the level
    itself)."""
    level = getattr(capture_config, "level", "off") or "off"
    since = getattr(capture_config, "enabled_at", "") or ""
    use = capture_mod.usage(corpus, pricing, since=since)
    weekly = capture_mod.weekly_cost(use)
    if h is None or h.effort_share_threshold_pct != 30.0:
        h = collect(corpus, pricing, ratings=ratings, tz=tz, since=since)
    value = capture_dependent_value(h, since=since)
    suggestion = capture_step_down_suggestion(h, capture_config, use)
    rows = [
        ["level", catalogue.LEVEL_TITLES.get(level, level)],
        ["since", since[:10] if since else ""],
        ["note_tokens", use.note_tokens],
        ["tag_tokens", use.tag_tokens],
        ["cost", use.cost],
        ["share", use.share],
        ["coverage", use.coverage],
        ["report_coverage", use.report_coverage],
        ["feedback_runs", use.feedback_runs],
        ["feedback_cost", use.feedback_cost],
        ["sessions_with_notes", use.sessions],
        ["after_compact_notes", use.after_compact_notes],
        ["after_compact_cost", use.after_compact_cost],
        ["weekly_cost", weekly],
        ["habit_value", value],
        ["held_back", 0],
        ["step_down_target", suggestion["target"] if suggestion else ""],
        [
            "step_down_tokens_saved",
            (suggestion["session_note_tokens_saved"] + suggestion["subagent_note_tokens_saved"])
            if suggestion else 0,
        ],
        ["step_down_weekly_saving", (suggestion["weekly_usd_saved"] or 0) if suggestion else 0],
    ]
    notes = [] if value is not None else [
        "Nothing measured yet relies on metrics capture or your feedback, so there's nothing to weigh its "
        "cost against."
    ]
    if suggestion is not None:
        target = suggestion["target"]
        dropped = [
            i for i in catalogue.level_metrics(level)
            if i not in catalogue.level_metrics(target) and catalogue.asks_claude(i)
        ]
        notes.append(
            f"Every metric {catalogue.LEVEL_TITLES[level]} adds over {catalogue.LEVEL_TITLES[target]} "
            f"has enough evidence of its own ({metric_list(dropped)}), and Claude's self-reports have settled. "
            f"Stepping down would save about {suggestion['session_note_tokens_saved']} tokens per session start "
            f"and {suggestion['subagent_note_tokens_saved']} per subagent start. "
            + step_down_terms(level, target)
        )
    table = Table(
        name="capture_usage",
        title="What metrics capture cost",
        columns=[Column(key="metric", label="Metric", kind="str"), Column(key="value", label="Value", kind="str")],
        rows=rows,
        notes=notes,
    )
    return Section(key="capture", title="Metrics capture", tables=[table])


def patch_capture_recommend_value(
    section: Section,
    corpus,
    pricing: Pricing | None,
    capture_config,
    *,
    ratings: dict | None = None,
    with_habits: list[Recommendation],
    without_habits: list[Recommendation],
    h: Habits | None = None,
    tz: str | tzinfo | None = None,
) -> Section:
    """EST-P7 + CAP-3: ``report.build_report`` calls this right after
    running ``recommend()`` twice (with and without the habits section),
    to fold what capture grows or unlocks there into the "capture"
    section's ``habit_value`` and add a ``held_back`` row for the
    recommendations capture's evidence argued away. ``section`` must be
    the one ``capture_section`` built (same corpus/pricing/config), since
    this recomputes the habits playbook rather than threading it through
    the whole report pipeline. ``h`` is reused under the same rule as
    :func:`capture_section` (S5/ROB-P3: one ``collect`` pass per report);
    ``tz`` as there."""
    since = getattr(capture_config, "enabled_at", "") or ""
    if h is None or h.effort_share_threshold_pct != 30.0:
        h = collect(corpus, pricing, ratings=ratings, tz=tz, since=since)
    items = playbook(h)
    value = capture_dependent_value(h, items, with_habits=with_habits, without_habits=without_habits, since=since)
    held_back = capture_value_breakdown(items, with_habits, without_habits)["held_back"]
    new_tables = []
    for table in section.tables:
        if table.name != "capture_usage":
            new_tables.append(table)
            continue
        new_rows = [
            (["held_back", held_back] if row and row[0] == "held_back"
             else ["habit_value", value] if row and row[0] == "habit_value"
             else row)
            for row in table.rows
        ]
        notes = list(table.notes)
        if value is None and not notes:
            notes = [
                "Nothing measured yet relies on metrics capture or your feedback, so there's nothing to weigh "
                "its cost against."
            ]
        elif value is not None and any("Nothing measured yet" in n for n in notes):
            notes = [n for n in notes if "Nothing measured yet" not in n]
        if held_back:
            plural = "s" if held_back != 1 else ""
            notes = [*notes, f"Capture's evidence held back {held_back} recommendation{plural} that would "
                              "otherwise be made. They are not counted as savings."]
        new_tables.append(replace(table, rows=new_rows, notes=notes))
    return replace(section, tables=new_tables)


__all__ = [
    "AgentFact",
    "CAPTURE_STEP_DOWN",
    "CycleFact",
    "DEFAULT_CHECKLISTS",
    "EXAMPLES",
    "Habits",
    "ITEMS",
    "Item",
    "MISSING_LINES",
    "Piece",
    "build_section",
    "capture_dependent_value",
    "capture_section",
    "collect",
    "confidence",
    "digest_table",
    "family",
    "item_title",
    "playbook",
    "playbook_table",
    "section_from",
    "template_lines",
    "trend",
    "unfit_agents",
    "went_well",
]

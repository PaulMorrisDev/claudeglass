"""The safe tuning export (Phase 7): a small JSON document of counts and
closed words that carries what a tuning review needs from one machine to
another, and nothing that names anything.

``build`` reads a corpus the way the dashboard does, each block through
the aggregator the dashboard already uses over the same window, and writes
only counts, list-price amounts and words from closed sets. ``validate``
checks a document against the spec tree below and fails closed: a key the
spec doesn't name, a string that isn't one of its words or tight patterns,
a number that isn't finite and 0 or more, a list where the spec wants an
object (or the reverse), a document over :data:`MAX_BYTES`, or any text in
the whole serialised document that looks like a path, an address or a URL
is a problem. ``summary_text`` and ``loads`` validate first, so a
file that fails the checks prints nothing.

What is never in it: names, paths, hashes, session ids, project slugs,
skill names, custom agent names (they count as ``custom``), model ids
(they count as a family), and any text of yours or Claude's. Every map's
keys come from a closed set built from the catalogues here, never from
the data, so a key can't carry anything either. A problem line names the
key path and the check, never the value or the key it found.

The spec tree is the single list of what may appear. A block is a function
that takes the shared :class:`_Ctx` and returns a dict, registered with
:func:`_block` beside the spec node that describes it; adding one means
adding one registration with ``required=False``, so documents made before
it still validate. The ``cost_centres``, ``project_files`` and
``compactions`` blocks (Phase 8) are such blocks, and the keys Phase 8 added
inside older blocks (``agents.probes``, ``agents.report_turns``,
``agents.launches``, ``agents.model_choice``,
``prompting.plans.builds`` and ``prompting.plans.asks``) are optional the
same way.
Money keys end in ``_usd`` and hold list-price amounts.

Window: the corpus the caller passes is the window (the callers load it with
``--days`` and put Haiku's tags on its turns with ``haiku_tags.apply``
first; ``build`` doesn't). ``days`` and ``today`` fill the header and bound
Haiku's judge calls (read from files here) and the overhead block's hook
runs and costs: the last ``days`` days, counting ``today`` whole, from local
midnight in the config's time zone as the corpus's window starts.

A map lists only the words that were counted, so a missing word reads as
zero. The caller passes the dashboard's session ratings and tip-card answers
(``ratings``, ``tip_feedback``, read from its store), so they count as
they do on the dashboard; only their counts reach the document.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterable

from . import PARSER_VERSION, __version__
from . import capture as capture_mod
from . import capture_catalogue as catalogue
from . import claude_md_review, classify
from . import compaction as compaction_mod
from . import context_budget as context_budget_mod
from . import context_files as context_files_mod
from . import cost_centres as cost_centres_mod
from . import discovery
from . import coaching as coaching_mod
from . import events as events_mod
from . import habits as habits_mod
from . import handoff as handoff_mod
from . import haiku_tags, helptext, hook_health, limits
from . import pieces as pieces_mod
from . import prompting as prompting_mod
from . import ratings as ratings_mod
from . import recache, recommend
from . import rework as rework_mod
from . import snapshots as snapshots_mod
from .calibration import Calibration
from .capture_tags import CHANGE_PATTERN, GROUNDED_KEYS
from .model import EventKind
from .pricing import price_turn
from .report import _agent_file_models, _dominant_transcript_model
from .topology import LAUNCH_WORDS

KIND = "claudeglass-tuning"
FORMAT = 1
#: The most a document may be, serialised (:func:`dumps`) as UTF-8. The
#: real ones are a few tens of kilobytes.
MAX_BYTES = 256 * 1024

#: No count or amount is anywhere near this; a bigger one is a mistake or
#: a forgery.
_MAX_NUMBER = 10**12
#: The most keys a map or items a list may hold.
_MAX_ITEMS = 200
#: The most weeks a weekly map holds (three years): ``build`` keeps the
#: latest when a long window has more. With every other map full, a
#: document of this many weeks still fits :data:`MAX_BYTES`.
_MAX_WEEKS = 160
#: More than the changes grounding can make (288), so none is ever left out.
_MAX_CHANGES = 300


class TuningError(ValueError):
    """A document that doesn't pass :func:`validate`. ``problems`` are its
    lines: a key path and the check it failed, never a value."""

    def __init__(self, problems: Iterable[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        shown = "; ".join(self.problems[:5])
        more = len(self.problems) - 5
        super().__init__(shown + (f"; and {more} more" if more > 0 else ""))


# -- closed sets, from the catalogues -----------------------------------------------------

#: Who wrote a tag: Claude in its reply, or Haiku (the tagger, or the
#: fallback for a reply Claude left without one).
WRITERS = ("claude", *catalogue.JUDGE_WRITERS)
LEVELS = (*catalogue.LEVELS, catalogue.CUSTOM_LEVEL)
#: The tips Claude is asked to relay.
HINTS = tuple(prompting_mod.TIP_HINTS)
HABITS = tuple(prompting_mod.HABITS)
#: Who caught a mistake Claude admitted.
CAUGHT = ("user", "self")
#: How an agent run's last reply answered: it ended on a call of an answer
#: tool (``structured``, a workflow agent's, or ``handback``) or on text.
ANSWER_SHAPES = ("structured", "handback", "text")
_SHAPE_OF = dict(zip(catalogue.AGENT_ANSWER_TOOLS, ("structured", "handback")))
#: What a run's verdict can be; ``none`` for a run that left no result word.
VERDICTS = (*catalogue.RESULT_WORDS, "none")
#: Session modes, as the rules pick them (an override set in sessions.toml
#: is free text and is never read here).
MODES = ("overnight", "long-agentic", "interactive", "one-shot", "mixed")
LEVEL_WORDS = (*catalogue.TAG_VOCAB["level"], pieces_mod.UNKNOWN)
MODELS = (*catalogue.AGENT_MODEL_TIERS, "other")
#: Built-in agent types by name; every other type is ``custom``.
AGENT_TYPES = (*sorted(recommend._BUILTIN_AGENT_TYPES | set(catalogue.SKIP_AGENT_TYPES)), "custom")
ENTRYPOINTS = (*helptext.TABLE_COPY["by_entrypoint"].value_labels, "other")
#: What a project file is, by its extension class (``unknown`` when it could
#: not be found on disk), how it reaches an agent, and its size in tokens.
FILE_EXTS = (*claude_md_review.EXT_CLASSES, "unknown")
FILE_SOURCES = context_files_mod.SOURCES
FILE_SIZES = ("to_1k", "to_2k", "to_5k", "to_10k", "to_20k", "over_20k")
_FILE_SIZE_BOUNDS = (1000, 2000, 5000, 10000, 20000)
#: Who has a file in a run: the main session or an agent type.
FILE_REACHES = ("main", *AGENT_TYPES)
#: The most files the export keeps.
FILES_KEPT = 40
#: The model-choice rows: who started the run, the model tiers the table
#: names, and who chose the model (a word each, ``not recorded`` with an
#: underscore). The export keeps the dearest ``MODEL_ROWS_KEPT``.
MODEL_STARTERS = ("direct", "workflow")
MODEL_TIERS = tuple(cost_centres_mod.TIER_LABELS)
MODEL_CHOSEN = tuple(word.replace(" ", "_") for word in cost_centres_mod.CHOSEN_LABELS)
MODEL_ROWS_KEPT = 60
#: How a build began after an approved plan, and the groups of plans put up
#: for an ask: the words of the Plan handoff and Work habits tables.
BUILD_STARTS = handoff_mod.START_WORDS
PLAN_ASKS = habits_mod.PLAN_ROUND_KINDS
#: The triggers of a summary; every other word counts as ``other``.
COMPACTION_TRIGGERS = ("auto", "manual", "other")
#: The events ClaudeGlass's own hooks run on.
HOOK_EVENTS = tuple(
    sorted(
        {
            event
            for _script, event, _matcher, _async in catalogue.hook_specs(
                (*catalogue.METRICS_BY_ID, catalogue.HAIKU_TAGGER_HOOK)
            )
        }
        | {"SubagentStart", "SessionStart"}
    )
)
#: Usage-limit stops by kind, in the words the dashboard uses.
LIMIT_KINDS = ("five_hour", "weekly")
_LIMIT_KIND_OF = {"session_limit": "five_hour", "weekly_limit": "weekly"}
#: Histogram buckets: rounds (of a piece, of a plan, of a run of small
#: requests), seconds between small requests, and unattended night hours.
COUNT_BUCKETS = ("one", "two", "three", "four_to_six", "seven_or_more")
_COUNT_BOUNDS = (1, 2, 3, 6)
GAP_BUCKETS = ("to_1m", "to_5m", "to_20m", "over_20m")
_GAP_BOUNDS = (60, 300, 1200)
HOUR_BUCKETS = ("to_1h", "to_2h", "to_4h", "to_8h", "over_8h")
_HOUR_BOUNDS = (1, 2, 4, 8)
#: A reply that starts at a background task's notice this long after the
#: reply before it is a wake-up (``habits.is_wake_up``, which the Work
#: habits report-turns table counts the same way).
WAKE_GAP_S = habits_mod.WAKE_GAP_S
#: The replies before a message whose cache writes say how long the cache
#: lasts, and the two lifetimes (the capture hook's rule).
_TTL_REPLIES = 5
_TTL_5M_S = 300
_TTL_1H_S = 3600

_WEEK_RE = re.compile(r"20\d\d-W(?:0[1-9]|[1-4]\d|5[0-3])")
_VERSION_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){1,3}(?:[-.]?(?:a|b|rc|dev|post)\d{1,3})?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CHANGE_RE = re.compile(CHANGE_PATTERN)


# -- the spec tree ----------------------------------------------------------------------------


def _at(path: str) -> str:
    return path or "document"


def _sub(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


class Node:
    """One thing the spec allows. ``check`` adds a line to ``problems``
    for each way ``value`` fails it; a line names the key path and the
    check, never the value."""

    def check(self, value: Any, path: str, problems: list[str]) -> None:
        raise NotImplementedError


class Count(Node):
    """A whole number, 0 or more."""

    def check(self, value, path, problems):
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{_at(path)}: must be a whole number")
        elif not 0 <= value <= _MAX_NUMBER:
            problems.append(f"{_at(path)}: must be 0 or more and not absurdly large")


class Num(Node):
    """A finite number, 0 or more (an amount in list-price USD, a time)."""

    def check(self, value, path, problems):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{_at(path)}: must be a number")
        elif isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{_at(path)}: must be a finite number")
        elif not 0 <= value <= _MAX_NUMBER:
            problems.append(f"{_at(path)}: must be 0 or more and not absurdly large")


class Const(Node):
    """Exactly this word or whole number."""

    def __init__(self, expected: str | int):
        self.expected = expected

    def check(self, value, path, problems):
        if type(value) is not type(self.expected) or value != self.expected:
            problems.append(f"{_at(path)}: is not the value this version of the format has")


class Word(Node):
    """One word of a closed set."""

    def __init__(self, words: Iterable[str]):
        self.words = frozenset(words)

    def check(self, value, path, problems):
        if not isinstance(value, str) or value not in self.words:
            problems.append(f"{_at(path)}: must be one of the words the spec allows")


class Pattern(Node):
    """A string that matches a tight pattern in full; ``valid`` is a last
    check on a string that does (a date that exists)."""

    def __init__(self, regex: re.Pattern, valid: Callable[[str], bool] | None = None):
        self.regex = regex
        self.valid = valid

    def check(self, value, path, problems):
        if (
            not isinstance(value, str)
            or self.regex.fullmatch(value) is None
            or (self.valid is not None and not self.valid(value))
        ):
            problems.append(f"{_at(path)}: must match the pattern the spec allows")


class Keys:
    """The keys a map may have."""

    def accepts(self, key: str) -> bool:
        raise NotImplementedError


class Closed(Keys):
    """Keys from a closed set of words."""

    def __init__(self, words: Iterable[str]):
        self.words = frozenset(words)

    def accepts(self, key):
        return key in self.words


class Weeks(Keys):
    """ISO weeks, ``2026-W31``."""

    def accepts(self, key):
        return _WEEK_RE.fullmatch(key) is not None


class Changes(Keys):
    """A grounding change, ``key:from>to``: a key grounding may change, and
    both words from that key's closed vocabulary (``to`` is empty for a
    word dropped)."""

    def accepts(self, key):
        if _CHANGE_RE.fullmatch(key) is None:
            return False
        name, _, change = key.partition(":")
        old, _, new = change.partition(">")
        vocab = catalogue.TAG_VOCAB.get(name, ())
        return name in GROUNDED_KEYS and old in vocab and (not new or new in vocab)


_CHANGES = Changes()


class Obj(Node):
    """An object with these keys and no others. ``required`` keys must be
    there; the rest may be absent."""

    def __init__(self, fields: dict[str, Node], required: Iterable[str] = ()):
        self.fields = dict(fields)
        self.required = tuple(required)

    def check(self, value, path, problems):
        if not isinstance(value, dict):
            problems.append(f"{_at(path)}: must be an object")
            return
        for key, item in value.items():
            if not isinstance(key, str) or key not in self.fields:
                # The key itself might be the leak, so it is never repeated.
                problems.append(f"{_at(path)}: has a key that is not in the spec")
                continue
            self.fields[key].check(item, _sub(path, key), problems)
        for key in self.required:
            if key not in value:
                problems.append(f"{_sub(path, key)}: is missing")


class Map(Node):
    """An object whose keys come from a closed set (``keys``: a collection
    of words or a :class:`Keys`) and whose values are all ``item``."""

    def __init__(self, keys: Keys | Iterable[str], item: Node, limit: int = _MAX_ITEMS):
        self.keys = keys if isinstance(keys, Keys) else Closed(keys)
        self.item = item
        self.limit = limit

    def check(self, value, path, problems):
        if not isinstance(value, dict):
            problems.append(f"{_at(path)}: must be an object")
            return
        if len(value) > self.limit:
            problems.append(f"{_at(path)}: has more entries than the spec allows")
            return
        for key, item in value.items():
            if not isinstance(key, str) or not self.keys.accepts(key):
                problems.append(f"{_at(path)}: has a key that is not in the spec")
                continue
            self.item.check(item, _sub(path, key), problems)


class Arr(Node):
    """A list of ``item``."""

    def __init__(self, item: Node, limit: int = _MAX_ITEMS):
        self.item = item
        self.limit = limit

    def check(self, value, path, problems):
        if not isinstance(value, list):
            problems.append(f"{_at(path)}: must be a list")
            return
        if len(value) > self.limit:
            problems.append(f"{_at(path)}: has more items than the spec allows")
            return
        for i, item in enumerate(value):
            self.item.check(item, f"{path}[{i}]", problems)


def _valid_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _counts(*names: str) -> Obj:
    """An object of these counts, all of them there."""
    return Obj({name: Count() for name in names}, required=names)


def _count_map(words: Iterable[str]) -> Map:
    return Map(words, Count())


_HEADER: dict[str, Node] = {
    "kind": Const(KIND),
    "format": Const(FORMAT),
    "tool_version": Pattern(_VERSION_RE),
    "parser_version": Count(),
    "generated_on": Pattern(_DATE_RE, _valid_date),
    "window_days": Count(),
}

#: ``name -> (spec node, builder, required)``, in the order the blocks are
#: written.
_BLOCKS: dict[str, tuple[Node, Callable[["_Ctx"], dict], bool]] = {}


def _block(name: str, node: Node, *, required: bool = True):
    """Register a block's builder with the spec node that describes it."""

    def register(builder):
        _BLOCKS[name] = (node, builder, required)
        return builder

    return register


def spec() -> Obj:
    """The whole spec tree: the header and every registered block."""
    return Obj(
        {**_HEADER, **{name: node for name, (node, _builder, _required) in _BLOCKS.items()}},
        required=(*_HEADER, *(name for name, (_n, _b, required) in _BLOCKS.items() if required)),
    )


# -- the second scan --------------------------------------------------------------------------

#: What may never appear anywhere in the serialised document, whatever the
#: spec says: the shapes of a path, an address or a URL.
_LEAKS = (
    ("a drive path", re.compile(r"[A-Za-z]:[\\/]")),
    ("a home folder path", re.compile(r"home/")),
    ("a Users folder path", re.compile(r"Users[\\/]")),
    ("a Git Bash drive path", re.compile(r"(?<![A-Za-z0-9])/[A-Za-z]/")),
    ("an at sign", re.compile(r"@")),
    ("a web address", re.compile(r"://|www\.")),
)


def _text(doc) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=True, allow_nan=False)


def validate(doc) -> list[str]:
    """The problems with ``doc``, one line each, ``[]`` for a valid one.
    A line names the key path and the check it failed and never repeats a
    value, so printing the list can't leak what it found."""
    problems: list[str] = []
    spec().check(doc, "", problems)
    try:
        text = _text(doc)
    except (TypeError, ValueError, RecursionError):
        problems.append("document: cannot be written as plain JSON")
        return problems
    if len(text.encode("utf-8")) > MAX_BYTES:
        problems.append("document: is over the size limit")
    for what, pattern in _LEAKS:
        if pattern.search(text):
            problems.append(f"document: holds {what}")
    return problems


def dumps(doc: dict) -> str:
    """``doc`` as the text a file holds, after :func:`validate` passes it;
    raises :class:`TuningError` otherwise."""
    problems = validate(doc)
    if problems:
        raise TuningError(problems)
    return _text(doc) + "\n"


def _no_repeated_keys(pairs: list[tuple[str, Any]]) -> dict:
    """An object :func:`loads` reads, refused when a key comes twice: the
    parser would keep the last value and drop the others unchecked."""
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise TuningError("file: has a key more than once")
    return dict(pairs)


def loads(text: str) -> dict:
    """The document in ``text``, validated; raises :class:`TuningError` when
    it is too big, isn't JSON, repeats a key in an object or fails
    :func:`validate`. The second scan reads ``text`` itself as well as the
    document written back, so nothing the parser dropped slips past it."""
    if len(text.encode("utf-8", errors="replace")) > 2 * MAX_BYTES:
        raise TuningError("file: is over the size limit")
    try:
        doc = json.loads(text, object_pairs_hook=_no_repeated_keys)
    except TuningError:
        raise
    except (ValueError, RecursionError):
        raise TuningError("file: is not JSON") from None
    problems = validate(doc)
    for what, pattern in _LEAKS:
        line = f"document: holds {what}"
        if line not in problems and pattern.search(text):
            problems.append(line)
    if problems:
        raise TuningError(problems)
    return doc


def read_file(path: str | Path) -> dict:
    """The document in the file at ``path``, validated. The size is checked
    before anything is read, so a huge file is refused unread."""
    path = Path(path)
    try:
        too_big = path.stat().st_size > 2 * MAX_BYTES
        text = "" if too_big else path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise TuningError("file: cannot be read as text") from None
    if too_big:
        raise TuningError("file: is over the size limit")
    return loads(text)


# -- what build reads -------------------------------------------------------------------------


def _num(value) -> float:
    """``value`` as a plain amount: 0 or more, finite, six places."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, number), 6) if math.isfinite(number) else 0.0


def _bucket(n: float, bounds: tuple[int, ...], words: tuple[str, ...]) -> str:
    """The word of the first bound ``n`` is within, else the last word."""
    for bound, word in zip(bounds, words):
        if n <= bound:
            return word
    return words[-1]


def _iso_week(monday: str) -> str:
    """``2026-W31`` for the Monday (``YYYY-MM-DD``) the dashboard's weeks
    start on; ``""`` for no week, or one the spec has no room for (a
    timestamp outside 2000 to 2099 is a bad clock, not a week)."""
    try:
        year, week, _ = date.fromisoformat(monday).isocalendar()
    except ValueError:
        return ""
    label = f"{year}-W{week:02d}"
    return label if _WEEK_RE.fullmatch(label) else ""


def _tally(counter, words: Iterable[str]) -> dict[str, int]:
    """``counter`` as a map over ``words``, in their order, leaving out any
    word that wasn't counted and anything that isn't one."""
    return {word: int(counter[word]) for word in words if counter.get(word)}


def _words_of(value) -> tuple:
    return value if isinstance(value, tuple) else (value,)


class _Session:
    """One main session: its bundle, prompt cycles and /cg-feedback
    answers, drawn once for every block."""

    def __init__(self, bundle):
        self.bundle = bundle
        self.top = bundle.top
        self.subs = list(bundle.subs)
        self.id = bundle.session_id
        self.workflows = getattr(bundle, "workflows", ())
        self.cycles = capture_mod.prompt_cycles(self.top, self.subs, self.workflows)
        self.spans = capture_mod.feedback_spans(self.cycles)
        self.replies = capture_mod._priced(self.top)


class _Ctx:
    """The shared reads behind the blocks, each worked out once when a
    block first asks for it."""

    def __init__(
        self, corpus, config, pricing, config_dir, days: int, today: date, claude_root, ratings=None, tip_feedback=None
    ):
        self.corpus = corpus
        self.config = config
        self.pricing = pricing
        self.config_dir = config_dir
        self.claude_root = claude_root
        self.days = days
        self.today = today
        self.ratings = ratings or {}
        self.tip_feedback = tip_feedback or {}
        # Local midnight in the config's zone, where the corpus's --days window starts.
        self.start = discovery.local_midnight(today - timedelta(days=days - 1), config.tz)

    @cached_property
    def sessions(self) -> list[_Session]:
        return [_Session(bundle) for bundle in self.corpus.sessions if bundle.top is not None]

    @cached_property
    def transcripts(self) -> list:
        return [result for s in self.sessions for result in (s.top, *s.subs)]

    @cached_property
    def calibration(self) -> Calibration:
        """Characters per token, measured on the subagent first calls as the
        report measures them."""
        return Calibration.from_calls(
            call for s in self.sessions for sub in s.subs if (call := context_budget_mod.first_call(sub))
        )

    @cached_property
    def startup(self) -> context_budget_mod.ContextBudgetStats:
        """What each subagent started with, fed as the report feeds it: a
        spawn that inherited the parent's conversation is told by the
        parent's context size on the turn that started it."""
        stats = context_budget_mod.ContextBudgetStats(calibration=self.calibration)
        for s in self.sessions:
            spawn_ctx = {
                tool_use_id: turn.ctx for tr in (s.top, *s.subs) for turn in tr.turns for tool_use_id in turn.tool_use_ids
            }
            for sub in s.subs:
                stats.add_subagent(
                    sub, spawn_ctx.get(sub.meta.tool_use_id) if sub.meta.tool_use_id else None, self.pricing
                )
        return stats

    @cached_property
    def context_files(self) -> dict:
        """The files that went into the runs (``context_files``), fed as the
        report feeds them; empty without a rate card to price them."""
        if self.pricing is None:
            return {}
        stats = context_files_mod.ContextFileStats(calibration=self.calibration)
        for s in self.sessions:
            for tr in (s.top, *s.subs):
                stats.add(tr, self.pricing, is_main=tr is s.top)
        return stats.to_dict()

    @cached_property
    def agent_files(self) -> dict[str, str | None]:
        """Each agent file's ``model`` line by agent type, from the newest
        settings snapshot with every project's agents, as the report reads
        them for the model-choice table; empty without a config folder or a
        snapshot. Read to tell who chose a model, never exported."""
        snapshots = snapshots_mod.load_snapshots(self.config_dir) if self.config_dir else []
        if not snapshots:
            return {}
        canonical = snapshots_mod.canonical_project_keys({b.slug for b in self.corpus.sessions if b.slug})
        return _agent_file_models(snapshots_mod.with_every_project_agents(snapshots, canonical))

    @cached_property
    def compactions(self) -> tuple[compaction_mod.CompactionStats, dict[str, float]]:
        """The summaries of every transcript, folded as the report folds
        them, and each main session's own cost (its agents cost is not in
        it) at list prices: what the Compactions section reads for the cost
        of summaries. The cost is empty without a rate card."""
        stats = compaction_mod.CompactionStats()
        thresholds = recache.RecacheThresholds.from_config(self.config.thresholds)
        main_cost: dict[str, float] = {}
        for s in self.sessions:
            for tr in (s.top, *s.subs):
                model = _dominant_transcript_model(tr)
                rates = self.pricing.resolve_model(model) if self.pricing is not None and model else None
                stats.add_transcript(tr, rates, thresholds)
            if self.pricing is not None:
                main_cost[s.top.meta.session_id] = sum(
                    price_turn(turn, self.pricing.resolve_model(turn.model)).total
                    for turn in s.top.turns
                    if turn.turn_index > 0
                )
        return stats, main_cost

    @cached_property
    def approvals(self) -> list[handoff_mod.PlanApproval]:
        """Every approved plan and how its build began
        (:func:`handoff.compute_handoff`); empty without a rate card."""
        if self.pricing is None:
            return []
        return handoff_mod.compute_handoff(self.transcripts, self.pricing).approvals

    @cached_property
    def habits(self) -> habits_mod.Habits:
        return habits_mod.collect(
            self.corpus,
            self.pricing,
            ratings=self.ratings,
            tz=self.config.tz,
            window=f"last {self.days} days",
            since=self.config.capture.enabled_at,
        )

    @cached_property
    def rework(self) -> rework_mod.Rework:
        return rework_mod.collect(self.habits)

    @cached_property
    def prompting(self) -> list[prompting_mod.SessionPrompting]:
        return prompting_mod.collect(self.corpus, self.pricing, ratings=self.ratings)

    @cached_property
    def thresholds(self) -> dict:
        personal = coaching_mod.read(self.config_dir) if self.config_dir else {}
        return ratings_mod.coaching_thresholds(self.config.thresholds, personal)

    @cached_property
    def mode_thresholds(self) -> dict:
        return classify.mode_and_purpose_thresholds_from_config(self.config.thresholds)[0]

    @cached_property
    def judged(self) -> dict[str, haiku_tags.Judged]:
        """Haiku's tagged main-session lines by reply id, the last line for
        a reply winning: who wrote a tag Haiku put on a turn."""
        if not self.config_dir:
            return {}
        return {j.reply: j for j in haiku_tags.load(self.config_dir) if j.kind == "main" and j.tag is not None}

    def writer_of(self, turn) -> str:
        """Who wrote a turn's tag: Claude, or the Haiku call that judged it."""
        cap = turn.cap
        if cap is None or not cap.judged:
            return "claude"
        judged = self.judged.get(turn.message_id)
        writer = judged.writer if judged is not None else catalogue.JUDGE_WRITER
        return writer if writer in WRITERS else catalogue.JUDGE_WRITER

    @cached_property
    def cycles_by_id(self) -> dict[tuple[str, int], Any]:
        """Each prompt cycle by the ``(session id, index)`` a piece of work
        names it with."""
        return {(s.id, i): cycle for s in self.sessions for i, cycle in enumerate(s.cycles)}

    @cached_property
    def piece_cycles(self) -> list[tuple[tuple[str, int], Any]]:
        """``(id, cycle)`` for each cycle of each piece of work."""
        return [
            (key, self.cycles_by_id[key])
            for piece in self.habits.work_pieces
            for key in piece.cycle_ids
            if key in self.cycles_by_id
        ]

    @cached_property
    def rework_keys(self) -> set[tuple[str, int]]:
        return {key for piece in self.habits.work_pieces for key in piece.rework_ids}


def build(
    corpus,
    config,
    pricing,
    *,
    config_dir,
    days: int,
    today: date,
    claude_root: str | Path | None = None,
    ratings: dict | None = None,
    tip_feedback: dict | None = None,
) -> dict:
    """The export document for ``corpus`` (already loaded for the last
    ``days`` days, Haiku's tags put on its turns). ``config_dir`` is where
    the tag files and ``coaching.json`` are; ``claude_root`` where
    settings.json is (the hooks it installs), Claude Code's own folder
    when not given. ``ratings`` are the dashboard's session ratings by
    session id (``Store.all_feedback``) and ``tip_feedback`` its tip-card
    answers (``Store.tip_feedback``), counted as the dashboard counts them;
    none of their words reach the document. Raises :class:`TuningError` if
    what it built doesn't validate, which would be a bug here."""
    days = max(1, int(days))
    ctx = _Ctx(corpus, config, pricing, config_dir, days, today, claude_root, ratings, tip_feedback)
    version = _VERSION_RE.match(__version__)
    doc: dict = {
        "kind": KIND,
        "format": FORMAT,
        "tool_version": version.group(0) if version else "0.0",
        "parser_version": int(PARSER_VERSION),
        "generated_on": today.isoformat(),
        "window_days": days,
    }
    for name, (_node, builder, _required) in _BLOCKS.items():
        doc[name] = builder(ctx)
    problems = validate(doc)
    if problems:
        raise TuningError(problems)
    return doc


# -- capture -----------------------------------------------------------------------------------


@_block(
    "capture",
    Obj(
        {
            "level": Word(LEVELS),
            "tagger": Word(catalogue.TAGGERS),
            "sample_percent": Count(),
            "metrics": Arr(Word(catalogue.METRICS_BY_ID), limit=len(catalogue.METRICS_BY_ID)),
            "coaching": Arr(Word(catalogue.COACHING_IDS), limit=len(catalogue.COACHING_IDS)),
            "thresholds": Map(catalogue.COACHING_THRESHOLDS, Num()),
            "hints": Map(HINTS, _counts("notes", "tips", "misfires", "useful", "known", "wrong")),
        },
        required=("level", "tagger", "sample_percent"),
    ),
)
def _capture(ctx: _Ctx) -> dict:
    """The setup (level, who tags, the share of sessions, the metrics and
    coaching on, the thresholds in force) and, per tip, how it went: the
    notes that asked for it, the replies that showed it, those Claude
    called a misfire (``Turn.tip_disowned``) and your answers (in
    /cg-feedback, a session rating or a tip card)."""
    cap = ctx.config.capture
    active = set(cap.active_metrics())
    notes: Counter = Counter()
    tips: Counter = Counter()
    misfires: Counter = Counter()
    answers: Counter = Counter()
    for s in ctx.prompting:
        notes.update(s.notes)
        tips.update(s.tips)
        misfires.update(s.misfires)
        answers.update(s.tip_answers)
    answers.update(prompting_mod.card_answers(ctx.tip_feedback))
    hints = {}
    for hint in HINTS:
        row = {
            "notes": notes[hint],
            "tips": tips[hint],
            "misfires": misfires[hint],
            "useful": answers[(hint, "useful")],
            "known": answers[(hint, "known")],
            "wrong": answers[(hint, "wrong")],
        }
        if any(row.values()):
            hints[hint] = row
    return {
        "level": cap.level if cap.level in LEVELS else "off",
        "tagger": cap.tagger if cap.tagger in catalogue.TAGGERS else catalogue.DEFAULT_TAGGER,
        "sample_percent": int(min(100, max(0, cap.sample))),
        "metrics": [m for m in catalogue.METRICS_BY_ID if m in active],
        "coaching": [c for c in catalogue.COACHING_IDS if c in cap.coaching],
        "thresholds": {key: _num(ctx.thresholds[key]) for key in catalogue.COACHING_THRESHOLDS if key in ctx.thresholds},
        "hints": hints,
    }


# -- prompting ---------------------------------------------------------------------------------

_TOTALS = ("typed", "queued", "go_aheads", "status_checks", "corrections", "adjustments", "reminders")
#: How a build began after an approved plan, and a group of plans put up
#: for an ask (``plans.builds``, ``plans.asks``).
_BUILD_SIDE = Obj(
    {
        "approvals": Count(),
        "typed": Count(),
        "carried_tokens": Count(),
        "replies": Count(),
        "context_tokens": Count(),
        "cost_usd": Num(),
    },
    required=("approvals", "replies", "cost_usd"),
)
_ASK_SIDE = Obj(
    {"plans": Count(), "typed": Count(), "sent_back": Count(), "asked": Count(), "tokens": Count(), "cost_usd": Num()},
    required=("plans", "sent_back", "asked", "cost_usd"),
)


@_block(
    "prompting",
    Obj(
        {
            "totals": _counts(*_TOTALS),
            "weeks": Map(
                Weeks(),
                Obj(
                    {"messages": Count(), "habits": _count_map(HABITS), "rates": Map(HABITS, Num())},
                    required=("messages", "habits"),
                ),
                limit=_MAX_WEEKS,
            ),
            "plans": Obj(
                {
                    "approved": Count(),
                    "rounds": _count_map(COUNT_BUCKETS),
                    "rejected_rounds": Count(),
                    "feedback_rounds": _count_map(catalogue.PLAN_FEEDBACK_CLASSES),
                    "builds": Map(BUILD_STARTS, _BUILD_SIDE),
                    "asks": Map(PLAN_ASKS, _ASK_SIDE),
                },
                required=("approved", "rounds", "rejected_rounds", "feedback_rounds"),
            ),
            "denials": _count_map(events_mod.DENIAL_BUCKETS),
        },
        required=("totals", "weeks", "plans", "denials"),
    ),
)
def _prompting(ctx: _Ctx) -> dict:
    """How you ask. ``weeks`` holds, per ISO week, the messages that asked
    for something (the dashboard's denominator), the times each habit
    showed and its rate per hundred messages (a week with fewer than
    ``TREND_MIN_MESSAGES`` has none). ``totals`` count the messages you
    typed, those you typed while Claude worked, and how they read.
    ``plans`` counts the pieces of work with an approved plan, the plan
    rounds each took, the rounds declined in the dialog
    (``rejected_rounds``, a decline answered with a go-ahead included) and
    how your feedback to them read; ``builds`` counts the approved plans
    by how the build began (``handoff.START_WORDS``: in the same session,
    after a /clear, or in a session that opens with the plan), with the
    replies of the build and what they cost; ``asks`` the plans put up for
    each ask, in the groups of the Plans sent back table
    (``habits.PLAN_ROUND_KINDS``).
    ``denials`` are the tool calls that were turned away."""
    tz = ctx.config.tz
    weeks: dict[str, dict] = {}

    def week_row(at) -> dict | None:
        week = _iso_week(habits_mod._week(at, tz))
        return weeks.setdefault(week, {"messages": 0, "habits": Counter()}) if week else None

    totals: Counter = Counter()
    for s in ctx.prompting:
        totals["typed"] += len(s.messages)
        for m in s.messages:
            row = week_row(m.at)
            if row is not None:
                row["messages"] += m.asks
        for o in s.occurrences:
            row = week_row(o.at)
            if row is not None and o.habit in HABITS:
                row["habits"][o.habit] += 1
    denials: Counter = Counter()
    rejected = 0
    feedback_classes: Counter = Counter()
    for s in ctx.sessions:
        for cycle in s.cycles:
            first = cycle.turns[0]
            totals["go_aheads"] += int(first.human_go)
            totals["status_checks"] += int(first.human_status)
            totals["corrections"] += int(first.human_correction)
            totals["adjustments"] += int(first.human_adjust)
            totals["reminders"] += int(first.human_remind)
            for turn in cycle.turns:
                totals["queued"] += turn.queued_prompts
                totals["go_aheads"] += int(turn.queued_go)
                totals["status_checks"] += int(turn.queued_status)
                totals["corrections"] += int(turn.queued_correction)
                totals["adjustments"] += int(turn.queued_adjust)
            rejected += cycle.rejected_rounds
            feedback_classes.update(cycle.plan_feedback_classes)
        for result in (s.top, *s.subs):
            for turn in result.turns:
                for bucket, n in turn.preceding_denials.items():
                    if bucket in events_mod.DENIAL_BUCKETS:
                        denials[bucket] += int(n)
    rounds: Counter = Counter()
    approved = 0
    cycles = ctx.cycles_by_id
    for piece in ctx.habits.work_pieces:
        if piece.plan_approved:
            approved += 1
            n = sum(cycles[key].plan_rounds for key in piece.cycle_ids if key in cycles)
            rounds[_bucket(max(1, n), _COUNT_BOUNDS, COUNT_BUCKETS)] += 1
    out_weeks = {}
    for week, row in sorted(weeks.items())[-_MAX_WEEKS:]:
        entry: dict = {"messages": row["messages"], "habits": _tally(row["habits"], HABITS)}
        if row["messages"] >= prompting_mod.TREND_MIN_MESSAGES:
            entry["rates"] = {
                habit: round(100.0 * row["habits"][habit] / row["messages"], 1) for habit in HABITS if row["habits"][habit]
            }
        out_weeks[week] = entry
    plans = {
        "approved": approved,
        "rounds": _tally(rounds, COUNT_BUCKETS),
        "rejected_rounds": rejected,
        "feedback_rounds": _tally(feedback_classes, catalogue.PLAN_FEEDBACK_CLASSES),
    }
    builds = {
        word: {
            "approvals": len(rows),
            "typed": sum(1 for a in rows if a.typed),
            "carried_tokens": sum(a.tokens_carried for a in rows),
            "replies": sum(a.build_turns for a in rows),
            "context_tokens": sum(a.build_context for a in rows),
            "cost_usd": _num(sum(a.build_usd for a in rows)),
        }
        for word in BUILD_STARTS
        if (rows := [a for a in ctx.approvals if a.start == word])
    }
    if builds:
        plans["builds"] = builds
    asks = {
        kind: {
            "plans": len(group),
            "typed": sum(1 for p in group if p.typed),
            "sent_back": sum(p.sent_back for p in group),
            "asked": sum(p.asked for p in group),
            "tokens": sum(p.tokens for p in group),
            "cost_usd": _num(sum(p.cost for p in group)),
        }
        for kind, group in habits_mod.plan_round_groups(ctx.habits).items()
    }
    if asks:
        plans["asks"] = asks
    return {
        "totals": {key: int(totals[key]) for key in _TOTALS},
        "weeks": out_weeks,
        "plans": plans,
        "denials": _tally(denials, events_mod.DENIAL_BUCKETS),
    }


# -- tags and feedback ------------------------------------------------------------------------

#: The questions whose answers are counted by word; ``missed_in`` has a
#: count of its own, by cycle.
_FEEDBACK_KEYS = tuple(k for k in catalogue.FEEDBACK_ANSWER_KEYS if k != "missed_in")
#: What is counted for each shift word Claude wrote.
_SHIFT_COUNTS = ("cycles", "corrections", "adjustments", "rework")


@_block(
    "tags",
    Obj(
        {
            "tagged": _count_map(WRITERS),
            "words": Obj(
                {
                    key: Obj({writer: _count_map(words) for writer in WRITERS})
                    for key, words in catalogue.TAG_VOCAB.items()
                }
            ),
            "grounding": Map(Changes(), Count(), limit=_MAX_CHANGES),
            "settling": Map(Changes(), Count(), limit=_MAX_CHANGES),
            "shift": Map(catalogue.TAG_VOCAB["shift"], _counts(*_SHIFT_COUNTS)),
            "admissions": Map(
                catalogue.TAG_VOCAB["admit"], Obj({caught: _count_map(WRITERS) for caught in CAUGHT})
            ),
            "unconfirmed_admissions": Count(),
            "feedback": Obj(
                {
                    "answered": Count(),
                    "skipped": Count(),
                    "answers": Obj({key: _count_map(catalogue.FEEDBACK_VOCAB[key]) for key in _FEEDBACK_KEYS}),
                    "mapped_from_text": _count_map(catalogue.FEEDBACK_ANSWER_KEYS),
                    "missed_in": _count_map(catalogue.FEEDBACK_VOCAB["missed_in"]),
                },
                required=("answered", "skipped", "answers", "mapped_from_text", "missed_in"),
            ),
        },
        required=("tagged", "words", "unconfirmed_admissions", "feedback"),
    ),
)
def _tags(ctx: _Ctx) -> dict:
    """The tags and what became of them. ``words`` counts each word of
    each key as written, by who wrote it. ``grounding`` and ``settling``
    count the words that were put right (by the hook in Haiku's, and by
    what the transcript shows), as ``key:from>to``. ``shift`` sets the
    shift Claude wrote against a correction or an adjustment you made and
    whether the message turned out to be rework. ``admissions`` count the
    mistakes Claude admitted by kind, who caught them and who wrote the
    tag. ``feedback`` counts your /cg-feedback answers."""
    tagged: Counter = Counter()
    words: dict[str, dict[str, Counter]] = {key: {w: Counter() for w in WRITERS} for key in catalogue.TAG_VOCAB}
    grounding: Counter = Counter()
    for s in ctx.sessions:
        for turn in s.replies:
            cap = turn.cap
            if cap is None or not cap.has_tl:
                continue
            writer = ctx.writer_of(turn)
            tagged[writer] += 1
            for key, vocab in catalogue.TAG_VOCAB.items():
                for word in _words_of(getattr(cap, key, None)):
                    if word in vocab:
                        words[key][writer][word] += 1
            grounding.update(item for item in cap.grounded if _CHANGES.accepts(item))
    settling: Counter = Counter()
    shift: dict[str, Counter] = {word: Counter() for word in catalogue.TAG_VOCAB["shift"]}
    admissions: dict[str, dict[str, Counter]] = {
        kind: {caught: Counter() for caught in CAUGHT} for kind in catalogue.TAG_VOCAB["admit"]
    }
    for key, cycle in ctx.piece_cycles:
        written, settled = cycle.tag, cycle.settled
        if written is not None and settled is not None:
            settling.update(item for item in settled.grounded[len(written.grounded):] if _CHANGES.accepts(item))
        if written is not None and written.shift in shift:
            opening = cycle.turns[0]
            row = shift[written.shift]
            row["cycles"] += 1
            row["corrections"] += int(opening.human_correction or any(t.queued_correction for t in cycle.turns))
            row["adjustments"] += int(opening.human_adjust or any(t.queued_adjust for t in cycle.turns))
            row["rework"] += int(key in ctx.rework_keys)
        kind = settled.admit if settled is not None else None
        if kind in admissions:
            admissions[kind][_caught_by(cycle)][_admission_writer(ctx, cycle)] += 1
    answered = skipped = 0
    answers: dict[str, Counter] = {key: Counter() for key in _FEEDBACK_KEYS}
    from_text: Counter = Counter()
    for s in ctx.sessions:
        for span in s.spans:
            fb = span.feedback
            if fb.source == "skipped":
                skipped += 1
                continue
            answered += 1
            for key in _FEEDBACK_KEYS:
                for word in _words_of(getattr(fb, key, None)):
                    if word in catalogue.FEEDBACK_VOCAB[key]:
                        answers[key][word] += 1
            from_text.update(key for key in fb.from_text if key in catalogue.FEEDBACK_ANSWER_KEYS)
    by_key = {}
    for key, vocab in catalogue.TAG_VOCAB.items():
        by_writer = {writer: _tally(words[key][writer], vocab) for writer in WRITERS}
        by_key[key] = {writer: counted for writer, counted in by_writer.items() if counted}
    return {
        "tagged": _tally(tagged, WRITERS),
        "words": {key: by_writer for key, by_writer in by_key.items() if by_writer},
        "grounding": dict(sorted(grounding.items())),
        "settling": dict(sorted(settling.items())),
        "shift": {
            word: {key: int(row[key]) for key in _SHIFT_COUNTS} for word, row in shift.items() if row["cycles"]
        },
        "admissions": {
            kind: {caught: _tally(by_writer, WRITERS) for caught, by_writer in rows.items() if by_writer}
            for kind, rows in admissions.items()
            if any(rows.values())
        },
        "unconfirmed_admissions": ctx.rework.admitted.possible,
        "feedback": {
            "answered": answered,
            "skipped": skipped,
            "answers": {
                key: _tally(counter, catalogue.FEEDBACK_VOCAB[key]) for key, counter in answers.items() if counter
            },
            "mapped_from_text": _tally(from_text, catalogue.FEEDBACK_ANSWER_KEYS),
            "missed_in": _tally(ctx.rework.missed_in, catalogue.FEEDBACK_VOCAB["missed_in"]),
        },
    }


def _caught_by(cycle) -> str:
    """Who found the mistake a cycle's reply admits: ``user`` when a message
    of yours pushed back first. The same rule as ``pieces._admission``."""
    caught = {t.admit_caught for t in cycle.tag_turns if t.admit_candidate and t.admit_caught}
    if caught:
        return "user" if "user" in caught else "self"
    opening = cycle.turns[0]
    pushed = (
        opening.human_correction
        or opening.human_adjust
        or any(t.queued_correction or t.queued_adjust for t in cycle.turns)
    )
    return "user" if pushed else "self"


def _admission_writer(ctx: _Ctx, cycle) -> str:
    """Who wrote the word that admitted a mistake: the writer of the last
    reply of the cycle that carried one, else whoever wrote the cycle's
    tags."""
    for turn in reversed(cycle.tag_turns):
        if turn.cap is not None and turn.cap.has_tl and turn.cap.admit:
            return ctx.writer_of(turn)
    return catalogue.JUDGE_WRITER if cycle.haiku_only else "claude"


# -- pieces and modes --------------------------------------------------------------------------


def _session_mode(ctx: _Ctx, session: _Session) -> tuple[str, float]:
    """``(mode, unattended night hours)`` the rules give a session. The
    same features and rule ``classify.classify_session`` uses, without an
    override."""
    features = classify.extract_features(
        session.top,
        session.subs,
        ctx.config.tz,
        workflows=len(session.workflows),
        entrypoint=session.top.meta.entrypoint,
        thresholds=ctx.mode_thresholds,
    )
    mode, _evidence = classify.classify_mode(features, ctx.mode_thresholds)
    return (mode if mode in MODES else classify.CATCH_ALL_MODE), features.unattended_night_s / 3600


def _cache_ttl_s(replies: list) -> int:
    """An hour when any recent reply wrote to the 1-hour cache, else five
    minutes: the capture hook's rule (``_cache_ttl_s``)."""
    return _TTL_1H_S if any(t.cc_1h > 0 for t in replies[-_TTL_REPLIES:]) else _TTL_5M_S


def _breaks(ctx: _Ctx) -> tuple[list[int], list[int]]:
    """``([cold returns, cache-write tokens], [wake-ups, cache-write
    tokens])`` over the main sessions' replies. No report-side count
    existed, so this mirrors the capture hook's. A cold return is a
    message you typed after a gap longer than the cache the reply before it
    wrote lasts (``_cold_hint``), with a context of ``cold_min_tokens`` or
    more, and not a wait for a usage limit or a gap across a compaction. A
    wake-up is a reply that starts at a background task's notice an hour or
    more after the reply before it. The tokens are the cache writes of the
    reply that followed."""
    cold, wake = [0, 0], [0, 0]
    minimum = ctx.thresholds["cold_min_tokens"]
    for s in ctx.sessions:
        before: list = []
        for turn in s.replies:
            if turn.is_synthetic or turn.estimated:
                continue
            at = capture_mod._moment(turn.ts)
            if at is None:
                continue
            last = before[-1] if before else None
            last_at = capture_mod._moment(last.ts) if last is not None else None
            if last is not None and last_at is not None:
                gap = (at - last_at).total_seconds()
                compacted = bool(
                    {EventKind.COMPACT_BOUNDARY, EventKind.COMPACT_SUMMARY}.intersection(turn.preceding_event_kinds)
                )
                if (
                    turn.human_prompt_chars is not None
                    and turn.gap_cause != "limit"
                    and not compacted
                    and gap > _cache_ttl_s(before)
                    and last.ctx + last.output_tokens >= minimum
                ):
                    cold[0] += 1
                    cold[1] += turn.cache_creation_tokens
                elif habits_mod.is_wake_up(turn, gap):
                    wake[0] += 1
                    wake[1] += turn.cache_creation_tokens
            before.append(turn)
    return cold, wake


@_block(
    "pieces",
    Obj(
        {
            "total": Count(),
            "delivered": Count(),
            "segmented": Count(),
            "reworked": Count(),
            "rework_cycles": Count(),
            "rework_usd": Num(),
            "rounds": _count_map(COUNT_BUCKETS),
            "rework_by_cause": Map(
                pieces_mod.CAUSES, Obj({"cycles": Count(), "cost_usd": Num()}, required=("cycles", "cost_usd"))
            ),
            "rework_by_level": Map(LEVEL_WORDS, _counts("pieces", "reworked", "requests", "rework_cycles")),
            "rework_by_week": Map(Weeks(), _counts("pieces", "reworked"), limit=_MAX_WEEKS),
            "modes": _count_map(MODES),
            "unattended_nights": _count_map(HOUR_BUCKETS),
            "drip_runs": Obj({"by_length": _count_map(COUNT_BUCKETS), "by_gap": _count_map(GAP_BUCKETS)}),
            "cold_returns": _counts("count", "cache_write_tokens"),
            "wake_ups": _counts("count", "cache_write_tokens"),
        },
        required=("total", "reworked", "cold_returns", "wake_ups"),
    ),
)
def _pieces(ctx: _Ctx) -> dict:
    """Pieces of work and how they went: how many needed changes after
    delivery and why, by the level Claude tagged them and by week; the
    sessions by mode; the unattended hours of the nights Claude worked
    alone; runs of small requests sent one at a time; the messages that
    came back after a break and the replies a background task woke, with
    what writing the cache again cost in tokens. ``rounds`` is how many
    requests a delivered piece took."""
    h, r = ctx.habits, ctx.rework
    rounds: Counter = Counter()
    for piece in h.work_pieces:
        if piece.delivered and not piece.unsegmented and piece.substantive >= 1:
            rounds[_bucket(piece.substantive, _COUNT_BOUNDS, COUNT_BUCKETS)] += 1
    by_cause: dict[str, list] = {}
    for row in r.causes:
        entry = by_cause.setdefault(row.cause, [0, 0.0])
        entry[0] += row.cycles
        entry[1] += row.cost
    by_week = {}
    for row in r.weeks:
        week = _iso_week(row.week)
        if week and row.pieces:
            by_week[week] = {"pieces": row.pieces, "reworked": row.reworked}
    modes: Counter = Counter()
    nights: Counter = Counter()
    for s in ctx.sessions:
        mode, hours = _session_mode(ctx, s)
        modes[mode] += 1
        if hours > 0:
            nights[_bucket(hours, _HOUR_BOUNDS, HOUR_BUCKETS)] += 1
    drip_length: Counter = Counter()
    drip_gap: Counter = Counter()
    for s in ctx.prompting:
        for run in prompting_mod._drip_runs(s.messages):
            drip_length[_bucket(len(run), _COUNT_BOUNDS, COUNT_BUCKETS)] += 1
            drip_gap[_bucket(max(s.messages[i].since or 0.0 for i in run), _GAP_BOUNDS, GAP_BUCKETS)] += 1
    cold, wake = _breaks(ctx)
    return {
        "total": len(h.work_pieces),
        "delivered": r.delivered,
        "segmented": r.rate.segmented,
        "reworked": r.rate.reworked,
        "rework_cycles": r.cycles,
        "rework_usd": _num(r.rate.rework_cost + r.unsegmented.rework_cost),
        "rounds": _tally(rounds, COUNT_BUCKETS),
        "rework_by_cause": {
            cause: {"cycles": by_cause[cause][0], "cost_usd": _num(by_cause[cause][1])}
            for cause in pieces_mod.CAUSES
            if cause in by_cause
        },
        "rework_by_level": {
            word: {
                "pieces": rate.pieces,
                "reworked": rate.reworked,
                "requests": rate.substantive,
                "rework_cycles": rate.rework,
            }
            for word, rate in r.levels.items()
            if word in LEVEL_WORDS
        },
        "rework_by_week": dict(sorted(by_week.items())[-_MAX_WEEKS:]),
        "modes": _tally(modes, MODES),
        "unattended_nights": _tally(nights, HOUR_BUCKETS),
        "drip_runs": {"by_length": _tally(drip_length, COUNT_BUCKETS), "by_gap": _tally(drip_gap, GAP_BUCKETS)},
        "cold_returns": {"count": cold[0], "cache_write_tokens": cold[1]},
        "wake_ups": {"count": wake[0], "cache_write_tokens": wake[1]},
    }


# -- agents ------------------------------------------------------------------------------------

_JUDGE_SIDE = Obj(
    {"calls": Count(), "tagged": Count(), "cost_usd": Num(), "errors": _count_map(catalogue.JUDGE_ERRORS)},
    required=("calls", "tagged", "cost_usd", "errors"),
)
_RUNS_SIDE = Obj({"runs": Count(), "cost_usd": Num()}, required=("runs", "cost_usd"))
#: What the runs started one way did (the Work habits run-receipts table).
_LAUNCH_SIDE = Obj(
    {
        "runs": Count(),
        "agents": Count(),
        "replies": Count(),
        "single": Count(),
        "start_reads": Count(),
        "compactions": Count(),
        "compacted": Count(),
        "shared_reads": Count(),
        "shared_usd": Num(),
        "cost_usd": Num(),
    },
    required=("runs", "agents", "replies", "cost_usd"),
)
#: One row of the model-choice table: runs of one agent type on one model
#: tier, whose model was chosen the same way.
_MODEL_CHOICE = Obj(
    {
        "started_by": Word(MODEL_STARTERS),
        "agent_type": Word(AGENT_TYPES),
        "model": Word(MODEL_TIERS),
        "chosen": Word(MODEL_CHOSEN),
        "runs": Count(),
        "cost_usd": Num(),
        "ceiling_usd": Num(),
    },
    required=("started_by", "agent_type", "model", "chosen", "runs", "cost_usd"),
)


@_block(
    "agents",
    Obj(
        {
            "verdicts": Map(ANSWER_SHAPES, _count_map(VERDICTS)),
            "raced": Count(),
            "judge": Obj({"main": _JUDGE_SIDE, "agent": _JUDGE_SIDE}, required=("main", "agent")),
            "runs": Obj({"direct": _RUNS_SIDE, "workflow": _RUNS_SIDE}, required=("direct", "workflow")),
            "types": _count_map(AGENT_TYPES),
            "models": Map(MODELS, _RUNS_SIDE),
            "startup_diet_usd": Map(AGENT_TYPES, Num()),
            "probes": Obj({"calls": Count(), "single": Count(), "by_shell": Count(), "runs": Count()}),
            "report_turns": _count_map(habits_mod.REPORT_KINDS),
            "launches": Map(LAUNCH_WORDS, _LAUNCH_SIDE),
            "model_choice": Arr(_MODEL_CHOICE, limit=MODEL_ROWS_KEPT),
        },
        required=("verdicts", "raced", "judge", "runs", "types", "models"),
    ),
)
def _agents(ctx: _Ctx) -> dict:
    """The agent runs. ``verdicts`` counts the result word each run left,
    by how its last reply answered; ``raced`` the runs whose answer had not
    reached the transcript when the judge looked. ``judge`` is Haiku's
    calls over the window, for your replies and for agent runs, the agent
    lines of older logs attributed by their reply. ``runs`` split the
    direct ones from a workflow's, with their list-price cost; ``types``
    count them by built-in type (everything else is ``custom``); ``models``
    by family. ``startup_diet_usd``, when any agent type would be given a
    tools list, is what that saves over the window at list prices, by the
    same types (:func:`_startup_diet_usd`). ``probes`` counts the
    agents' replies, those that made one read-only call and nothing else,
    those by shell command, and the runs of two or more in a row.
    ``report_turns`` counts the main session's replies to a background
    agent's or a workflow's report by what they did
    (``habits.REPORT_KINDS``). ``launches`` count the runs by how they were
    started (``topology.LAUNCH_WORDS``): the agents and
    replies, the single read-only calls, the starting context
    times the replies, the summaries made inside the runs, the files a
    sibling had already read, and the cost, so a cost per spawn is a
    division. ``model_choice`` is the Agents page's model-choice table, a row
    per who started the run, agent type (custom ones as ``custom``), model
    tier and who chose the model, with the runs, their cost and the most that
    Sonnet could save, the dearest ``MODEL_ROWS_KEPT`` of them."""
    shapes: dict[str, Counter] = {shape: Counter() for shape in ANSWER_SHAPES}
    for s in ctx.sessions:
        if not s.replies:
            continue
        for sub in s.subs:
            priced = capture_mod._priced(sub)
            if not priced:
                continue
            names = set(priced[-1].tool_names)
            shape = next((word for tool, word in _SHAPE_OF.items() if tool in names), "text")
            result = next((t.result_marker for t in reversed(priced) if t.result_marker), None)
            shapes[shape][result if result in catalogue.RESULT_WORDS else "none"] += 1
    runs = {"direct": [0, 0.0], "workflow": [0, 0.0]}
    types: Counter = Counter()
    models: dict[str, list] = {}
    for fact in ctx.habits.agents:
        side = runs["direct" if fact.direct else "workflow"]
        side[0] += 1
        side[1] += fact.cost
        types[fact.agent_type if fact.agent_type in AGENT_TYPES else "custom"] += 1
        family = models.setdefault(
            habits_mod.family(fact.model) if habits_mod.family(fact.model) in catalogue.AGENT_MODEL_TIERS else "other",
            [0, 0.0],
        )
        family[0] += 1
        family[1] += fact.cost
    agent_ids = haiku_tags.agent_replies(ctx.corpus)
    judge = {}
    for side in ("main", "agent"):
        seen = haiku_tags.summary(ctx.config_dir, since=ctx.start, kind=side, agent_reply_ids=agent_ids)
        judge[side] = {
            "calls": seen.calls,
            "tagged": seen.tagged,
            "cost_usd": _num(seen.usd),
            "errors": _tally(seen.errors, catalogue.JUDGE_ERRORS),
        }
    block = {
        "verdicts": {shape: _tally(counter, VERDICTS) for shape, counter in shapes.items() if counter},
        "raced": judge["agent"]["errors"].get("no_answer", 0),
        "judge": judge,
        "runs": {side: {"runs": n, "cost_usd": _num(cost)} for side, (n, cost) in runs.items()},
        "types": _tally(types, AGENT_TYPES),
        "models": {word: {"runs": models[word][0], "cost_usd": _num(models[word][1])} for word in MODELS if word in models},
    }
    diet = _startup_diet_usd(ctx.startup)
    if diet:
        block["startup_diet_usd"] = diet
    agents = ctx.habits.agents
    if any(fact.probe_calls for fact in agents):
        block["probes"] = {
            "calls": sum(fact.calls for fact in agents),
            "single": sum(fact.probe_calls for fact in agents),
            "by_shell": sum(fact.probe_shell_calls for fact in agents),
            "runs": sum(fact.probe_runs for fact in agents),
        }
    if ctx.habits.report_turns:
        block["report_turns"] = _tally(Counter(r.kind for r in ctx.habits.report_turns), habits_mod.REPORT_KINDS)
    launches = {
        word: {
            "runs": len({a.run or id(a) for a in group}),
            "agents": len(group),
            "replies": sum(a.calls for a in group),
            "single": sum(a.probe_calls for a in group),
            "start_reads": sum(a.start_tokens * a.calls for a in group),
            "compactions": sum(a.compactions for a in group),
            "compacted": sum(1 for a in group if a.compactions),
            "shared_reads": sum(a.shared_reads for a in group),
            "shared_usd": _num(sum(a.shared_cost for a in group)),
            "cost_usd": _num(sum(a.cost for a in group)),
        }
        for word, group in habits_mod.agent_run_groups(ctx.habits).items()
    }
    if launches:
        block["launches"] = launches
    choices = _model_choice(ctx)
    if choices:
        block["model_choice"] = choices
    return block


def _model_choice(ctx: _Ctx) -> list[dict]:
    """The model-choice table's rows (:func:`cost_centres.model_choice`)
    as words and amounts: a custom agent type is ``custom``, a run's model
    a tier, who chose it a word. Rows that fall on the same words add up.
    Without a rate card nothing can be priced, so there are none."""
    if ctx.pricing is None:
        return []
    found = cost_centres_mod.model_choice([(s.top, s.subs) for s in ctx.sessions], ctx.pricing, ctx.agent_files)
    merged: dict[tuple[str, str, str, str], list] = {}
    for (centre, agent_type, tier, chosen), row in found.items():
        key = (
            centre,
            agent_type if agent_type in AGENT_TYPES else "custom",
            tier if tier in MODEL_TIERS else "unknown",
            chosen.replace(" ", "_"),
        )
        item = merged.setdefault(key, [0, 0.0, 0.0])
        item[0] += row.runs
        item[1] += row.cost
        if row.cost_on_sonnet > 0:
            item[2] += max(row.cost - row.cost_on_sonnet, 0.0)
    rows = []
    for (centre, agent_type, tier, chosen), (runs, cost, ceiling) in sorted(
        merged.items(), key=lambda item: (-item[1][1], item[0])
    )[:MODEL_ROWS_KEPT]:
        row = {"started_by": centre, "agent_type": agent_type, "model": tier, "chosen": chosen, "runs": runs}
        row["cost_usd"] = _num(cost)
        if ceiling > 0:
            row["ceiling_usd"] = _num(ceiling)
        rows.append(row)
    return rows


def _startup_diet_usd(stats) -> dict[str, float]:
    """What a tools list is worth over the window, by agent type: the
    saving of each ``agent_startup_diet`` row the dashboard would give a
    tools list card for (:func:`recommend.tools_list_offer`), at list
    prices. Built-in types by name, every other type summed as ``custom``.
    Amounts only: no agent's name, tool or file is in it."""
    section = context_budget_mod.build_startup_section(stats)
    table = next((t for t in section.tables if t.name == "agent_startup_diet"), None)
    saved: Counter = Counter()
    for row in table.rows if table is not None else []:
        values = dict(zip((column.key for column in table.columns), row))
        usd = values.get("saving_usd")
        if recommend.tools_list_offer(values["agent_type"], values) is None or not isinstance(usd, (int, float)):
            continue
        saved[values["agent_type"] if values["agent_type"] in AGENT_TYPES else "custom"] += usd
    return {word: _num(saved[word]) for word in AGENT_TYPES if saved.get(word, 0) > 0}


# -- overhead ----------------------------------------------------------------------------------

_LIMIT_FIELDS = Obj(
    {
        "episodes": Count(),
        "kept_working": Count(),
        "cut_off_direct": Count(),
        "cut_off_workflow": Count(),
        "cut_off_usd": Num(),
    },
    required=("episodes", "kept_working", "cut_off_direct", "cut_off_workflow", "cut_off_usd"),
)


@_block(
    "overhead",
    Obj(
        {
            "entrypoints": _count_map(ENTRYPOINTS),
            "hooks": Map(
                HOOK_EVENTS,
                Obj({"runs": Count(), "recorded": Count(), "median_ms": Num()}, required=("runs", "recorded")),
            ),
            "capture_usd": Num(),
            "coaching_usd": Num(),
            "limits": Map(LIMIT_KINDS, _LIMIT_FIELDS),
        },
        required=("entrypoints", "hooks", "capture_usd", "coaching_usd", "limits"),
    ),
)
def _overhead(ctx: _Ctx) -> dict:
    """What the tool and the limits cost. ``entrypoints`` count sessions
    by how they were started. ``hooks`` are the runs ClaudeGlass's own
    hooks made, for the events whose runs the transcripts show (counted
    from what fires them), how many Claude Code recorded a time for, and
    the median of those. ``hooks``, ``capture_usd`` and ``coaching_usd``
    count from when capture was turned on when that was inside the window,
    as the Capture page's overhead line does. ``limits`` count the
    usage-limit stops by kind, the weekly ones that didn't stop work, and
    the agents each cut off."""
    entry: Counter = Counter()
    for s in ctx.sessions:
        word = s.top.meta.entrypoint or "unknown"
        entry[word if word in ENTRYPOINTS else "other"] += 1
    # The Capture page's rule (capture_view.overhead_window): from when
    # capture was turned on, when that was inside the window, since its
    # hooks weren't there before.
    since = ctx.start
    cap = ctx.config.capture
    enabled = capture_mod._moment(cap.enabled_at) if cap.is_on and cap.enabled_at else None
    if enabled is not None and enabled > since:
        since = enabled
    since_text = since.isoformat(timespec="seconds")
    hooks = {}
    measured = hook_health.measure_hook_overhead(
        ctx.transcripts, hook_health.installed_specs(ctx.claude_root), since=since_text
    )
    for row in measured.rows:
        # An event the transcripts can't count (a wait, a permission prompt,
        # an API error) holds only its recorded runs: unknown, not zero.
        if row.event not in HOOK_EVENTS or not row.counted:
            continue
        item = {"runs": row.runs, "recorded": row.recorded}
        if row.median_ms is not None:
            item["median_ms"] = _num(row.median_ms)
        hooks[row.event] = item
    use = capture_mod.usage(ctx.corpus, ctx.pricing, since=since_text)
    coaching = capture_mod.coaching_usage(ctx.corpus, ctx.pricing, since=since_text)
    stats = limits.LimitStats()
    lookup = ctx.pricing.resolve_model if ctx.pricing is not None else None
    for result in ctx.transcripts:
        stats.add(result, lookup)
    stops: dict[str, Counter] = {kind: Counter() for kind in LIMIT_KINDS}
    cut_usd: Counter = Counter()
    for episode in stats.episodes():
        kind = _LIMIT_KIND_OF.get(episode.kind)
        if kind is None:
            continue
        stops[kind]["episodes"] += 1
        stops[kind]["kept_working"] += int(kind == "weekly" and episode.kept_working)
        stops[kind]["cut_off_direct"] += episode.cut_off_direct
        stops[kind]["cut_off_workflow"] += episode.cut_off_workflow
        cut_usd[kind] += episode.cut_off_cost_usd
    return {
        "entrypoints": _tally(entry, ENTRYPOINTS),
        "hooks": {event: hooks[event] for event in HOOK_EVENTS if event in hooks},
        "capture_usd": _num(use.cost),
        "coaching_usd": _num(coaching.cost),
        "limits": {
            kind: {
                **{
                    key: int(stops[kind][key])
                    for key in ("episodes", "kept_working", "cut_off_direct", "cut_off_workflow")
                },
                "cut_off_usd": _num(cut_usd[kind]),
            }
            for kind in LIMIT_KINDS
            if stops[kind]["episodes"]
        },
    }


@_block(
    "cost_centres",
    Map(
        cost_centres_mod.CENTRES,
        Obj({f"{cell}_usd": Num() for cell in cost_centres_mod.CELLS}),
    ),
    required=False,
)
def _cost_centres(ctx: _Ctx) -> dict:
    """Where the spend went (the Agents page's cost-centre table): for each
    cost centre (the main session, direct agents, workflow agents, session
    starts), its list-price spend in each cell of the matrix (base read,
    above-base read, growth write, rewrite, post-compaction, output). A
    cost centre or cell with no spend is left out. Amounts only: no names,
    and no parts of the base, which are worked out from your own prompts.
    Without a rate card nothing can be priced, so the block is empty."""
    if ctx.pricing is None:
        return {}
    found = cost_centres_mod.compute(
        [(s.top, s.subs) for s in ctx.sessions],
        ctx.pricing,
        recache.RecacheThresholds.from_config(ctx.config.thresholds),
    )
    return {
        centre: {f"{cell}_usd": _num(usd) for cell, usd in cells.items()}
        for centre, cells in cost_centres_mod.matrix_usd(found).items()
    }


# -- project files -----------------------------------------------------------------------------

_PROJECT_FILE = Obj(
    {
        "ext": Word(FILE_EXTS),
        "source": Word(FILE_SOURCES),
        "size": Word(FILE_SIZES),
        "weekly": Map(Weeks(), Word(FILE_SIZES), limit=context_files_mod.SERIES_WEEKS),
        "reach": Map(FILE_REACHES, Num()),
    },
    required=("ext", "source", "size", "reach"),
)


@_block(
    "project_files",
    Obj({"total": Count(), "files": Arr(_PROJECT_FILE, limit=FILES_KEPT)}, required=("total", "files")),
    required=False,
)
def _project_files(ctx: _Ctx) -> dict:
    """The files that go into agents' runs (the Agents page's project-files
    table): the CLAUDE.md files Claude Code loads, the files they import and
    the files agents read by habit. For each of the dearest ``FILES_KEPT``:
    its extension class, how it arrives (``auto``, ``import`` or ``read``),
    its size now and each recent week's as a bucket, and, for the main
    session and each agent type, the share of its runs that had the file.
    Never a name, a path or a hash: the names the dashboard shows are worked
    out on disk and only the extension class is kept. A custom agent's runs
    count as ``custom``. ``total`` counts every file, kept or not."""
    data = ctx.context_files
    if not data:
        return {"total": 0, "files": []}
    if ctx.config_dir:
        rows, _local = claude_md_review.project_file_rows(ctx.config_dir, data)
    else:
        rows = context_files_mod.project_files(data)
    newest = context_files_mod.monday_of(data.get("newest") or "")
    files = []
    for row in context_files_mod.dearest(rows, FILES_KEPT):
        series = row["series"]
        weekly = {}
        for i, tokens in enumerate(series):
            week = _iso_week((newest - timedelta(weeks=len(series) - 1 - i)).isoformat()) if newest else ""
            if week:
                weekly[week] = _bucket(tokens, _FILE_SIZE_BOUNDS, FILE_SIZES)
        reach: dict[str, float] = {}
        for item in row["reach"]:
            word = item["reach"] if item["reach"] in FILE_REACHES else "custom"
            reach[word] = max(reach.get(word, 0.0), _num(item["share"]))
        files.append(
            {
                "ext": row["ext"] if row["ext"] in FILE_EXTS else ("md" if row["source"] == "auto" else "unknown"),
                "source": row["source"],
                "size": _bucket(row["tokens"], _FILE_SIZE_BOUNDS, FILE_SIZES),
                "weekly": weekly,
                "reach": {word: round(reach[word], 2) for word in FILE_REACHES if word in reach},
            }
        )
    return {"total": len(rows), "files": files}


# -- compactions -------------------------------------------------------------------------------


@_block(
    "compactions",
    Obj(
        {
            "sessions": Count(),
            "compacted": Count(),
            "summaries": Count(),
            "triggers": _count_map(COMPACTION_TRIGGERS),
            "in_agents": Obj({"subagent": Count(), "workflow": Count()}),
            "dropped_tokens": Count(),
            "write_usd": Num(),
            "summary_usd": Num(),
            "heavy": Obj({"sessions": Count(), "cost_usd": Num(), "main_usd": Num()}, required=("sessions", "cost_usd", "main_usd")),
        },
        required=("sessions", "compacted", "summaries"),
    ),
    required=False,
)
def _compactions(ctx: _Ctx) -> dict:
    """The conversation summaries Claude Code made (the Compactions
    section): the main sessions and how many of them summarised at all,
    the summaries in their conversations and how they were triggered (any
    word but ``auto`` and ``manual`` is ``other``), those made inside
    agent runs, the tokens all of them dropped, and, with a rate card, what they
    cost at list prices: the cache write on the reply after each summary,
    the estimated request that wrote it, and, for the sessions that
    summarised ``compaction.HEAVY_COMPACTIONS`` times or more, their
    sessions, the cost of their main conversations (``cost_usd``) and the
    cost of every main conversation (``main_usd``), so the share is a
    division. Counts and amounts: no session, no summary text."""
    stats, main_cost = ctx.compactions
    in_agents = {
        "subagent": stats.agent_compactions.get("subagent", 0),
        "workflow": stats.agent_compactions.get("workflow-agent", 0),
    }
    triggers: Counter = Counter()
    for record in stats.records:
        if record.kind not in compaction_mod.AGENT_KINDS:
            triggers[record.trigger if record.trigger in COMPACTION_TRIGGERS else "other"] += 1
    block: dict = {
        "sessions": stats.total_sessions,
        "compacted": stats.sessions_with_compaction,
        "summaries": len(stats.records) - sum(stats.agent_compactions.values()),
        "triggers": _tally(triggers, COMPACTION_TRIGGERS),
        "in_agents": in_agents,
        "dropped_tokens": stats.dropped_total,
    }
    if ctx.pricing is not None:
        block["write_usd"] = _num(stats.total_post_compaction_write_cost)
        block["summary_usd"] = _num(stats.summary_request_cost)
        heavy = stats.heavy_session_cost_share(main_cost)
        if heavy is not None and heavy[0]:
            block["heavy"] = {"sessions": heavy[0], "cost_usd": _num(heavy[1]), "main_usd": _num(stats.main_cost_total(main_cost))}
    return block


# -- the plain-words summary -------------------------------------------------------------------

_VERDICT_LABELS = {"none": "no word"}


def _label(word: str) -> str:
    return _VERDICT_LABELS.get(word, word.replace("_", " "))


def _n(count: int, one: str, many: str = "") -> str:
    return f"{count:,} {one if count == 1 else many or one + 's'}"


def _usd(amount: float) -> str:
    """An amount in dollars: cents, or four places for under a cent so a
    small figure doesn't read as nothing."""
    return f"${amount:.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def _list(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _ranked(counts: dict) -> list[tuple[str, int]]:
    """``counts`` biggest first, ties in word order."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _top(counts: dict, limit: int = 5) -> str:
    """``word n`` for the ``limit`` biggest counts, as a list."""
    return _list([f"{_label(word)} {n:,}" for word, n in _ranked(counts)[:limit]])


def _total(node) -> int:
    """Every count under ``node``, added up."""
    if isinstance(node, dict):
        return sum(_total(v) for v in node.values())
    return int(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else 0


def summary_text(doc) -> str:
    """Plain lines about ``doc``, a block each, for the summary command.
    Validates first and raises :class:`TuningError`, so a file that fails
    the checks prints nothing."""
    problems = validate(doc)
    if problems:
        raise TuningError(problems)
    lines = [
        f"Tuning figures made on {doc['generated_on']} for the last {_n(doc['window_days'], 'day')}, "
        f"by ClaudeGlass {doc['tool_version']}."
    ]
    for lines_of, name in (
        (_capture_lines, "capture"),
        (_prompting_lines, "prompting"),
        (_tags_lines, "tags"),
        (_pieces_lines, "pieces"),
        (_agents_lines, "agents"),
        (_overhead_lines, "overhead"),
        (_cost_centres_lines, "cost_centres"),
        (_project_files_lines, "project_files"),
        (_compactions_lines, "compactions"),
    ):
        if doc.get(name):
            lines += lines_of(doc[name])
    return "\n".join(lines)


def _capture_lines(block: dict) -> list[str]:
    if block["level"] == "off":
        lines = ["Capture: off."]
    else:
        lines = [
            f"Capture: level {block['level']}, tags written by {block['tagger']}, "
            f"{block['sample_percent']}% of sessions, {_n(len(block.get('metrics', [])), 'metric')} on."
        ]
    hints = (block.get("hints") or {}).values()
    if hints:
        notes, shown, missed = (sum(h.get(key, 0) for h in hints) for key in ("notes", "tips", "misfires"))
        lines.append(f"Tips: {_n(notes, 'note')} asked for one, {shown:,} shown, {missed:,} called a misfire.")
        useful, known, wrong = (sum(h.get(key, 0) for h in hints) for key in ("useful", "known", "wrong"))
        if useful + known + wrong:
            lines.append(
                f"You rated {_n(useful + known + wrong, 'tip')}: {useful:,} useful, {known:,} known and {wrong:,} wrong."
            )
    return lines


def _prompting_lines(block: dict) -> list[str]:
    totals = block["totals"]
    lines = [f"Prompting: {_n(totals['typed'], 'message')} typed, plus {totals['queued']:,} typed while Claude worked."]
    lines.append(
        f"How they read: {_n(totals['go_aheads'], 'go-ahead')}, {_n(totals['status_checks'], 'status check')}, "
        f"{_n(totals['corrections'], 'correction')}, {_n(totals['adjustments'], 'adjustment')} "
        f"and {_n(totals['reminders'], 'reminder')}."
    )
    plans = block["plans"]
    if plans["approved"] or plans["rejected_rounds"]:
        lines.append(f"Plans: {plans['approved']:,} approved, {_n(plans['rejected_rounds'], 'round')} declined in the dialog.")
    builds = plans.get("builds")
    if builds:
        total = sum(row["approvals"] for row in builds.values())
        lines.append(
            f"Builds after an approved plan: {_top({word: row['approvals'] for word, row in builds.items()})}, "
            f"{_n(total, 'plan')} in all."
        )
    denials = block["denials"]
    if denials:
        lines.append(f"Turned away: {_n(_total(denials), 'tool call')} ({_top(denials, 4)}).")
    habits: Counter = Counter()
    for week in block["weeks"].values():
        habits.update(week.get("habits", {}))
    if habits:
        lines.append(
            f"Habits showed {_n(sum(habits.values()), 'time')} over {_n(len(block['weeks']), 'week')}, "
            f"most often {_top(dict(habits), 3)}."
        )
    return lines


def _tags_lines(block: dict) -> list[str]:
    lines = []
    tagged = block.get("tagged") or {}
    if tagged:
        lines.append(f"Tags: {_n(sum(tagged.values()), 'reply', 'replies')} tagged, by {_top(tagged, 3)}.")
    grounded, settled = _total(block.get("grounding")), _total(block.get("settling"))
    if grounded or settled:
        lines.append(
            f"Words were put right {_n(grounded, 'time')} by the hook and {_n(settled, 'time')} by the transcript."
        )
    admissions = block.get("admissions") or {}
    unconfirmed = block.get("unconfirmed_admissions", 0)
    if admissions or unconfirmed:
        caught = {who: _total({k: v[who] for k, v in admissions.items() if who in v}) for who in CAUGHT}
        lines.append(
            f"Claude admitted {_n(_total(admissions), 'mistake')}: you caught {caught['user']:,} and it caught "
            f"{caught['self']:,}. {_n(unconfirmed, 'more reply', 'more replies')} read like admissions no tag confirmed."
        )
    feedback = block["feedback"]
    if feedback["answered"] or feedback["skipped"]:
        lines.append(f"Feedback: {feedback['answered']:,} answered, {feedback['skipped']:,} skipped.")
    return lines


def _pieces_lines(block: dict) -> list[str]:
    lines = [f"Pieces of work: {block['total']:,}."]
    if block.get("segmented"):
        # The Rework view's own rate: only pieces whose start was found
        # inside their session can be counted as reworked.
        lines.append(
            f"Of the {_n(block['segmented'], 'delivered piece')} with a clear start, "
            f"{block['reworked']:,} needed changes after delivery."
        )
    if block.get("rework_cycles"):
        causes = {cause: row["cycles"] for cause, row in (block.get("rework_by_cause") or {}).items()}
        common = f", most often {_label(_ranked(causes)[0][0])}" if causes else ""
        lines.append(
            f"Rework: {_n(block['rework_cycles'], 'follow-up')} after delivery, "
            f"{_usd(block.get('rework_usd', 0.0))} at list prices{common}."
        )
    if block.get("modes"):
        lines.append(f"Sessions by mode: {_top(block['modes'])}.")
    nights = _total(block.get("unattended_nights"))
    if nights:
        lines.append(f"{_n(nights, 'session')} had some night time with Claude working alone.")
    drips = _total((block.get("drip_runs") or {}).get("by_length"))
    if drips:
        lines.append(f"Small requests came one at a time in {_n(drips, 'run')}.")
    cold, wake = block["cold_returns"], block["wake_ups"]
    if cold["count"] or wake["count"]:
        lines.append(
            f"Cold returns: {cold['count']:,}, rewriting {cold['cache_write_tokens']:,} cache tokens. "
            f"Wake-ups: {wake['count']:,}, rewriting {wake['cache_write_tokens']:,}."
        )
    return lines


def _agents_lines(block: dict) -> list[str]:
    runs = block["runs"]
    lines = [
        f"Agent runs: {runs['direct']['runs']:,} direct costing {_usd(runs['direct']['cost_usd'])}, "
        f"{runs['workflow']['runs']:,} from workflows costing {_usd(runs['workflow']['cost_usd'])}."
    ]
    verdicts: Counter = Counter()
    for shape in block["verdicts"].values():
        verdicts.update(shape)
    if verdicts:
        lines.append(f"Agent answers: {_top(dict(verdicts))}.")
    judge = block["judge"]
    if judge["main"]["calls"] or judge["agent"]["calls"]:
        errors = Counter(judge["main"]["errors"]) + Counter(judge["agent"]["errors"])
        signed_out = errors.pop("no_login", 0)
        failed = sum(errors.values())
        lines.append(
            f"Haiku judged {_n(judge['main']['calls'], 'reply', 'replies')} and "
            f"{_n(judge['agent']['calls'], 'agent run')} for "
            f"{_usd(judge['main']['cost_usd'] + judge['agent']['cost_usd'])}, and {failed:,} failed."
        )
        if signed_out:
            lines.append(
                f"{_n(signed_out, 'call')} found the claude command signed out: sign in to the claude command in a terminal."
            )
    if block["types"]:
        lines.append(f"Agent types: {_top(block['types'])}.")
    if block["models"]:
        lines.append(f"Agent models: {_top({word: row['runs'] for word, row in block['models'].items()})}.")
    diet = block.get("startup_diet_usd")
    if diet:
        ranked = sorted(diet.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        lines.append(
            f"A tools list on your agents would save about {_usd(sum(diet.values()))} over the window at list prices. "
            f"By type: {_list([f'{_label(word)} {_usd(usd)}' for word, usd in ranked])}."
        )
    probes = block.get("probes")
    if probes:
        lines.append(
            f"Of {_n(probes['calls'], 'agent reply', 'agent replies')}, {probes['single']:,} made one read-only call "
            f"and nothing else, {probes['by_shell']:,} of them by shell command. "
            f"They came in {_n(probes['runs'], 'stretch', 'stretches')} of two or more in a row."
        )
    reports = block.get("report_turns")
    if reports:
        lines.append(f"Replies to an agent's report: {_top(reports)}.")
    launches = block.get("launches")
    if launches:
        parts = [
            f"{_label(word)} {_n(row['agents'], 'agent')} at {_usd(row['cost_usd'] / row['agents'])} each"
            for word, row in launches.items()
            if row["agents"]
        ]
        lines.append(f"Agents by how they were started: {_list(parts)}.")
    choices = block.get("model_choice")
    if choices:
        above = sum(row["runs"] for row in choices if row["model"] in ("opus", "fable"))
        ceiling = sum(row.get("ceiling_usd", 0.0) for row in choices)
        chosen = Counter()
        for row in choices:
            chosen[row["chosen"]] += row["runs"]
        lines.append(
            f"Model choice: {_n(above, 'agent run')} on Opus or above; chosen by {_top(dict(chosen), 4)}. "
            f"Sonnet could save up to {_usd(ceiling)} at list prices."
        )
    return lines


def _overhead_lines(block: dict) -> list[str]:
    lines = []
    if block["entrypoints"]:
        lines.append(f"Sessions started from: {_top(block['entrypoints'])}.")
    hooks = block["hooks"]
    if hooks:
        # Timed events first, so a measured median is never crowded out
        # by events Claude Code recorded no time for.
        ranked = sorted(hooks.items(), key=lambda kv: ("median_ms" not in kv[1], -kv[1]["runs"], kv[0]))
        parts = [
            f"{event} ran {row['runs']:,} times"
            + (f", median {row['median_ms']:g} ms" if "median_ms" in row else ", no time recorded")
            for event, row in ranked
        ]
        lines.append(f"Hooks: {'; '.join(parts)}.")
    lines.append(
        f"Capture cost {_usd(block['capture_usd'])} and coaching notes {_usd(block['coaching_usd'])} at list prices."
    )
    stops = block["limits"]
    if stops:
        parts = [
            _n(stops[kind]["episodes"], f"{kind.replace('_', '-')} stop")
            for kind in LIMIT_KINDS
            if kind in stops
        ]
        lines.append(f"Limits: {_list(parts)}.")
        kept = sum(row["kept_working"] for row in stops.values())
        if kept:
            lines.append(f"{_n(kept, 'weekly stop')} did not stop work.")
        cut = sum(row["cut_off_direct"] + row["cut_off_workflow"] for row in stops.values())
        if cut:
            lines.append(
                f"Agent runs cut off by a limit: {cut:,}, worth {_usd(sum(row['cut_off_usd'] for row in stops.values()))}."
            )
    return lines


def _cost_centres_lines(block: dict) -> list[str]:
    totals = {centre: sum(cells.values()) for centre, cells in block.items()}
    if not totals:
        return []
    parts = [
        f"{cost_centres_mod.CENTRE_LABELS[centre].lower()} {_usd(amount)}"
        for centre, amount in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return [f"Spend by cost centre, at list prices: {_list(parts)}."]


def _compactions_lines(block: dict) -> list[str]:
    if not block.get("summaries") and not any((block.get("in_agents") or {}).values()):
        return []
    lines = [
        f"Summaries: {_n(block['summaries'], 'summary', 'summaries')} in {_n(block['compacted'], 'session')} "
        f"of {block['sessions']:,}"
        + (f" ({_top(block['triggers'], 3)})" if block.get("triggers") else "")
        + "."
    ]
    inside = block.get("in_agents") or {}
    if any(inside.values()):
        lines.append(f"Inside agent runs: {_n(sum(inside.values()), 'summary', 'summaries')}.")
    if "write_usd" in block:
        lines.append(
            f"They cost {_usd(block['write_usd'])} in cache written on the next reply and about "
            f"{_usd(block.get('summary_usd', 0.0))} to write, at list prices."
        )
    heavy = block.get("heavy")
    if heavy:
        share = f", {100.0 * heavy['cost_usd'] / heavy['main_usd']:.0f}% of the total" if heavy["main_usd"] else ""
        lines.append(
            f"{_n(heavy['sessions'], 'session')} summarised {compaction_mod.HEAVY_COMPACTIONS} times or more: "
            f"{_usd(heavy['cost_usd'])} of main-session cost{share}."
        )
    return lines


def _project_files_lines(block: dict) -> list[str]:
    files = block["files"]
    if not files:
        return []
    read = sum(1 for item in files if item["source"] == "read")
    big = sum(1 for item in files if item["size"] in ("to_20k", "over_20k"))
    wide = sum(1 for item in files if sum(1 for word in item["reach"] if word != "main") >= 3)
    lines = [f"Project files agents take in: {_n(block['total'], 'file')}, {read:,} of the biggest read by habit."]
    extras = []
    if big:
        extras.append(f"{big:,} over 10,000 tokens")
    if wide:
        extras.append(f"{wide:,} used by 3 or more agent types")
    if extras:
        lines.append(f"Of those, {_list(extras)}.")
    return lines


__all__ = [
    "FORMAT",
    "KIND",
    "MAX_BYTES",
    "TuningError",
    "build",
    "dumps",
    "loads",
    "read_file",
    "spec",
    "summary_text",
    "validate",
]

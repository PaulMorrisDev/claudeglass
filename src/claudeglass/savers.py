"""Third-party token-saver tool ROI (v4-saver-roi): does the MCP server,
plugin, or skill someone installed to "save tokens" actually save any --
once its own overhead is paid for?

Motivation: an increasing number of MCP servers, plugins and skills claim
to cut token usage -- context compressors, log suppressors, "delegate
simple tasks to a local LLM" servers, structural/SQL-backed code-
navigation servers that stand in for `Grep`/`Read`, memory/knowledge-
graph servers. The tool's name is never known in advance (the owner's
own employer runs one on a work machine this project cannot see), so
detection has to be generic rather than a hardcoded allowlist -- see
:func:`detect_savers`. Every other analytic in this codebase measures
*this project's own* token spend; this module is the one place that
measures whether an *installed* claimed-saver is actually paying for
itself, net of the overhead it itself adds (schema/prefix load, its own
turns, its own tool-result chars).

Current state: nothing calls this module. ``report.build_report`` does
not add the ``savers`` section, ``recommend.recommend`` does not run
:data:`RULES`, and no CLI command or dashboard tab shows it; it is
library code with its own tests. It stays out of the report because its
output is not yet sound enough to show or act on:

- detection is a name match, so ordinary tools whose names contain
  "context", "memory" or "cache" (a documentation server, a notes
  server) are treated as savers;
- a saver configured for every session leaves no "absent" sessions to
  compare with, and when both groups exist they differ in workload, so
  the verdict cannot separate the tool from the work;
- the rule's lever, ``mcpServers.<name>``, is not a settings key the
  report's fixes can change (MCP servers live in ``.mcp.json`` or
  ``~/.claude.json``), and its action text writes dollar amounts
  directly instead of following the billing mode (``units.Units``).

Three layers, mirroring ``model_swap.py``/``carry.py``'s own shape:

- :func:`detect_savers` -- merges an explicit ``config.toml`` ``[savers]``
  allowlist with auto-detection: every MCP server name (``Turn.
  attribution_mcp_server``, and the server half of an
  ``mcp__<server>__<tool>`` tool name), every configured MCP server name
  and enabled plugin name from a config snapshot, and every
  ``Turn.attribution_skill`` value, checked against a fixed
  case-insensitive regex (``_SAVER_NAME_PATTERN``) for a name that reads
  as a saver ("token", "cache", "compress", "memory", ...). Both
  detection paths are merged and every candidate records how it was
  found (:class:`SaverCandidate.detected_via`).
- :func:`compute_saver_roi` -- for every detected candidate: its own
  **overhead** (turns/cost/tool-result chars attributed to it, and a
  schema-footprint proxy), its **effect** on cost/tokens/re-cache/
  compactions/turns in sessions where it was present versus absent
  (stratified by purpose/mode, gated on a minimum sample per arm), its
  **search-substitution** profile (native `Grep`/`Read`/`Glob`/shell-
  search calls versus the saver's own tool calls, and each side's carry
  cost -- see the owner's own working theory that their employer's saver
  is a code-search replacement), and a **verdict**: net saving per
  session = (absent-arm cost/session - present-arm cost/session) -
  overhead/session, labelled "observed, not controlled" throughout (the
  same causality caveat ``compare.py``'s own module docstring states).
- :func:`build_section` -- renders a finished :class:`SaverStats` into
  the ``savers`` report section's five tables. :data:`RULES` (rule id
  ``saver-tool-roi``) reads that rendered section back (never the raw
  stats -- the same "rules only read the finished report" convention
  every other standalone ``RULES`` module in this codebase follows) and
  recommends keeping (naming a result-size lever if overhead eats a
  large share of the gross saving) or disabling a saver whose net saving
  clears the opposite side of the threshold.

Deviations from the brief, reported rather than made silently (project
convention -- see ``model.py``'s own module docstring):

- **Where the explicit ``[savers]`` allowlist lives at the
  ``compute_saver_roi``/``build_section`` call sites.** The brief's
  ``detect_savers(results, snapshots, config) -> list[SaverCandidate]``
  signature takes a ``Config`` directly, but ``compute_saver_roi``'s own
  required signature -- ``(results, sessions, snapshots, rates,
  thresholds)`` -- has no ``config`` parameter for the allowlist to ride
  in on. Rather than have ``compute_saver_roi`` silently drop the
  explicit-allowlist path, :class:`SaverThresholds` carries a
  ``configured_names`` field, populated by :meth:`SaverThresholds.
  from_config` from ``config.savers`` (mirroring
  ``RecommendThresholds.from_config(data, config)``'s own two-parameter
  shape, which already threads ``config.min_sessions``/``min_turns``
  through the same way). ``detect_savers`` and ``compute_saver_roi``
  both funnel into the same private :func:`_detect` so the two entry
  points can never disagree about what counts as a candidate.
- **``compute_saver_roi`` has no ``config``/recache-thresholds
  parameter**, so re-cache detection (needed for the ``re-cache share``
  effect metric) runs at ``recache.RecacheThresholds()``'s packaged
  defaults rather than a caller's ``[thresholds]`` override -- the same
  "no config parameter, so use the packaged default" posture
  ``model_swap.compute_model_swap`` takes for a threshold it has no
  parameter to receive either.
- **The search-substitution table's carry-cost columns reuse
  ``carry.compute_carry`` directly** rather than reimplementing a
  token-count-times-remaining-turns estimate: ``carry.py`` is present in
  this repository and already prices exactly this ("what did leaving
  this tool result sitting in context cost, turn after turn, until a
  compaction or the transcript's end") per tool name, at
  ``price_turn``-accurate dollar figures rather than a flat
  chars/4-tokens-times-mean-turns proxy. ``compute_carry`` is called once
  per (saver, arm) over that arm's own flattened transcripts, so the
  reused figure is scoped to exactly the present/absent split this
  module needs, not the whole corpus at once.
- **Grep/Glob/Read calls are counted per (turn, tool) pair (``carry.
  compute_carry``'s own ``CarriedResult`` grain -- see that module's
  "Granularity note"), but a Bash/PowerShell search call is counted per
  *turn*, not per call** -- the frozen ``Turn`` contract keeps only the
  first shell command of a turn as ``cmd_prefix`` (redacted, <=40
  chars), so a turn invoking `rg` twice is indistinguishable from one
  invoking it once. This mixed granularity is stated in
  :data:`ASSUMPTIONS` and in the ``savers_search_substitution`` table's
  own notes rather than silently presented as a single consistent unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

from . import carry, recache
from . import snapshots as snapshots_mod
from .model import Column, EventKind, Recommendation, ReportModel, Section, SessionRecord, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn
from .snapshots import Snapshot, managed_keys

if TYPE_CHECKING:
    from .config import Config

#: No tokenizer is run over transcript content (privacy rule); tool-result
#: sizes are only ever known in characters. Duplicated from
#: ``topology._CHARS_PER_TOKEN_APPROX`` (also duplicated by
#: ``carry.py``/``context_budget.py``) rather than imported -- this
#: project's own established convention for this one constant.
_CHARS_PER_TOKEN_APPROX = 4

#: Fixed, case-insensitive: a name matching this reads as a claimed
#: token-saver tool. Deliberately not user-configurable (the brief states
#: this exact pattern) -- an explicit ``config.toml`` ``[savers]`` entry
#: is the escape hatch for a saver whose name gives no lexical hint.
_SAVER_NAME_PATTERN = re.compile(
    r"token|saver|savior|optimi[sz]|compress|context|memory|cache|lean|trim|condens",
    re.IGNORECASE,
)

#: An MCP tool's name is ``mcp__<server>__<tool>`` -- the server half
#: never itself contains ``__`` (observed convention throughout this
#: codebase's own tool names, e.g. ``mcp__ccd_session__dismiss_task``),
#: so the first ``__``-delimited segment after ``mcp__`` is the server.
_MCP_TOOL_PREFIX_RE = re.compile(r"^mcp__(.+?)__")

#: Native code-search tools a structural/SQL-backed search saver might
#: displace (owner's working theory -- see module docstring). ``Bash``/
#: ``PowerShell`` are handled separately via :data:`_SHELL_SEARCH_RE`
#: since a shell call's tool name alone doesn't say what it ran.
_NATIVE_SEARCH_TOOLS: tuple[str, ...] = ("Grep", "Glob", "Read")

#: A Bash/PowerShell turn counts as a native search call when its
#: (redacted, <=40 char) ``cmd_prefix`` starts with one of these.
_SHELL_SEARCH_RE = re.compile(r"^\s*(rg|grep|find|select-string)\b", re.IGNORECASE)

#: This module's own modelling assumptions -- for a caller that renders
#: :func:`build_section` to fold into ``ReportMeta.assumptions`` alongside ``ttl.ASSUMPTIONS``/
#: ``carry.ASSUMPTIONS``/``limits.ASSUMPTIONS`` (same convention).
ASSUMPTIONS: tuple[str, ...] = (
    "a saver 'name' matching the fixed regex token|saver|savior|"
    "optimi[sz]|compress|context|memory|cache|lean|trim|condens "
    "(case-insensitive) is a lexical heuristic, not a claim the tool "
    "actually does anything -- an explicit config.toml [savers] entry "
    "is the only way to track a saver whose name gives no hint",
    "presence in a session is 'this saver's own name appears in that "
    "session's joined config snapshot (mcp_servers.names or "
    "enabled_plugins), or at least one turn in the session is directly "
    "attributed to it' -- a saver configured but never actually invoked "
    "in a given session still counts as present there",
    "the present/absent split is observed, not controlled: the two "
    "groups of sessions differ in workload too, so a cost difference is "
    "not proof the saver caused it (same causality caveat as compare.py)",
    "tool-result sizes are chars / 4 (this project's standing "
    "chars-to-tokens approximation), not a real tokenizer",
    "the search-substitution table's carry-cost columns are computed "
    "by carry.compute_carry over just that (saver, arm)'s own "
    "transcripts -- the same per-turn read/write pricing model the "
    "carry report section uses, not a token-count proxy",
    "a Bash/PowerShell native-search call is counted once per turn "
    "(the frozen Turn contract keeps only that turn's first shell "
    "command as cmd_prefix), while Grep/Glob/Read calls come from "
    "carry.compute_carry's own per-(turn, tool) grain -- the two counts "
    "are not the same unit and are never added silently across that "
    "boundary without saying so",
    "re-cache detection inside this module always runs at "
    "recache.RecacheThresholds()'s packaged defaults -- compute_saver_roi "
    "has no config/recache-thresholds parameter to receive an override",
)


def _mcp_server_from_tool_name(tool_name: str) -> str | None:
    match = _MCP_TOOL_PREFIX_RE.match(tool_name)
    return match.group(1) if match else None


def _priced_turns(turns: Sequence[Turn]) -> list[Turn]:
    """Duplicated one-line helper -- see ``topology._priced_turns``'s/
    ``carry._priced_turns``'s identical docstring for why this project
    duplicates rather than imports it."""
    return [t for t in turns if t.turn_index > 0]


def _turn_attributed_to(turn: Turn, name: str) -> bool:
    """Whether ``turn`` is directly attributable to saver ``name``: the
    harness's own ``attribution_mcp_server``/``attribution_skill``
    fields, or an ``mcp__<name>__*`` tool call in this turn."""
    if turn.attribution_mcp_server == name or turn.attribution_skill == name:
        return True
    for tool_name in turn.tool_names:
        if _mcp_server_from_tool_name(tool_name) == name:
            return True
    return False


# -- detection ----------------------------------------------------------


@dataclass(slots=True)
class SaverCandidate:
    """One detected (or explicitly configured) candidate token-saver
    tool -- an MCP server, plugin, or skill name."""

    name: str
    #: True when ``name`` appears verbatim in ``config.toml``'s
    #: ``[savers] names`` list.
    from_config: bool = False
    #: Every independent source ``name`` was observed matching the
    #: detection regex in, sorted: any of ``"attribution_mcp_server"``,
    #: ``"attribution_skill"``, ``"mcp_tool_prefix"``,
    #: ``"snapshot_mcp_servers"``, ``"snapshot_enabled_plugins"``. Empty
    #: when ``name`` was found only via the explicit config list.
    detected_via: tuple[str, ...] = ()


def _detect(
    results: Sequence[TranscriptResult],
    snapshots: Sequence[Snapshot],
    configured_names: Sequence[str],
) -> list[SaverCandidate]:
    configured = {str(n) for n in configured_names if str(n)}
    sources: dict[str, set[str]] = {}

    def _note(raw_name: object, source: str) -> None:
        if not raw_name:
            return
        name = str(raw_name)
        if _SAVER_NAME_PATTERN.search(name):
            sources.setdefault(name, set()).add(source)

    for result in results:
        for turn in result.turns:
            _note(turn.attribution_mcp_server, "attribution_mcp_server")
            _note(turn.attribution_skill, "attribution_skill")
            for tool_name in turn.tool_names:
                _note(_mcp_server_from_tool_name(tool_name), "mcp_tool_prefix")

    for snap in snapshots:
        mcp_servers = snap.data.get("mcp_servers")
        if isinstance(mcp_servers, dict):
            for name in mcp_servers.get("names") or []:
                _note(name, "snapshot_mcp_servers")
        for plugin in snap.data.get("enabled_plugins") or []:
            _note(plugin, "snapshot_enabled_plugins")

    names = set(sources) | configured
    return [
        SaverCandidate(
            name=name,
            from_config=name in configured,
            detected_via=tuple(sorted(sources.get(name, ()))),
        )
        for name in sorted(names)
    ]


def detect_savers(
    results: Sequence[TranscriptResult],
    snapshots: Sequence[Snapshot],
    config: "Config | None",
) -> list[SaverCandidate]:
    """Every candidate token-saver tool: ``config.toml``'s explicit
    ``[savers] names`` list, merged with auto-detection over ``results``
    (``Turn.attribution_mcp_server``/``attribution_skill``, and
    ``mcp__<server>__<tool>`` tool-name prefixes) and ``snapshots``
    (``mcp_servers.names``, ``enabled_plugins``) -- see the module
    docstring. Sorted by name.
    """
    configured = tuple(getattr(config, "savers", None) or ())
    return _detect(results, snapshots, configured)


# -- overhead -------------------------------------------------------------


@dataclass(slots=True)
class SaverOverhead:
    """What a detected saver cost the corpus directly, on its own turns
    and tool calls."""

    name: str
    #: Turns directly attributed to this saver (see :func:`_turn_attributed_to`).
    turns: int = 0
    #: Sum of those turns' own priced cost.
    cost_usd: float = 0.0
    #: Tool calls to this saver's own ``mcp__<name>__*`` tools
    #: (``TranscriptResult.tool_result_calls``, corpus-wide).
    calls: int = 0
    #: Total chars of tool-result content those calls returned into
    #: context.
    result_chars: int = 0
    mean_result_chars: float | None = None
    #: Count of distinct ``mcp__<name>__*`` tool names observed -- a
    #: proxy for this saver's prefix-loaded schema footprint. Schema
    #: bytes themselves are not observable anywhere in this codebase.
    distinct_tool_names: int = 0


def _compute_overhead(
    candidates: Sequence[SaverCandidate],
    results: Sequence[TranscriptResult],
    rates: Pricing,
) -> dict[str, SaverOverhead]:
    names = [c.name for c in candidates]
    overhead = {name: SaverOverhead(name=name) for name in names}
    distinct_tools: dict[str, set[str]] = {name: set() for name in names}

    for result in results:
        for turn in _priced_turns(result.turns):
            for name in names:
                if not _turn_attributed_to(turn, name):
                    continue
                acc = overhead[name]
                acc.turns += 1
                acc.cost_usd += price_turn(turn, rates.resolve_model(turn.model)).total
            for tool_name in turn.tool_names:
                server = _mcp_server_from_tool_name(tool_name)
                if server in distinct_tools:
                    distinct_tools[server].add(tool_name)
        for tool_name, chars in result.tool_result_chars.items():
            server = _mcp_server_from_tool_name(tool_name)
            if server in overhead:
                overhead[server].result_chars += chars
                overhead[server].calls += result.tool_result_calls.get(tool_name, 0)
                distinct_tools[server].add(tool_name)

    for name, acc in overhead.items():
        acc.distinct_tool_names = len(distinct_tools[name])
        acc.mean_result_chars = (acc.result_chars / acc.calls) if acc.calls else None
    return overhead


# -- per-session aggregation (shared by effect + search-substitution) -----


@dataclass(slots=True)
class _SessionAgg:
    session_id: str
    mode: str
    purpose: str
    cost: float = 0.0
    priced_turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    recache_cc: int = 0
    compactions: int = 0
    tool_result_tokens: int = 0
    shell_search_turns: int = 0
    present: frozenset = frozenset()
    joined_snapshot: Snapshot | None = None
    transcripts: list = field(default_factory=list)


def _build_session_aggs(
    sessions: Sequence[SessionRecord],
    snapshots: Sequence[Snapshot],
    candidate_names: Sequence[str],
    rates: Pricing,
    recache_th: "recache.RecacheThresholds",
) -> list[_SessionAgg]:
    names = set(candidate_names)
    aggs: list[_SessionAgg] = []
    for record in sessions:
        if record.top is None:
            continue
        transcripts = [record.top, *record.subs]

        # ``project_key`` is every key the project's snapshots can carry
        # (``snapshots.snapshot_project_keys``), so either drive-letter
        # spelling joins.
        joined = snapshots_mod.snapshot_for(record.first_ts, snapshots, record.project_key) if record.first_ts and snapshots else None
        present: set[str] = set()
        if joined is not None:
            mcp_servers = joined.data.get("mcp_servers")
            if isinstance(mcp_servers, dict):
                present.update(str(n) for n in (mcp_servers.get("names") or []))
            present.update(str(p) for p in (joined.data.get("enabled_plugins") or []))
        present &= names

        classification = record.classification
        agg = _SessionAgg(
            session_id=record.session_id,
            mode=(classification.mode if classification else ""),
            purpose=(classification.purpose if classification else ""),
            joined_snapshot=joined,
            transcripts=transcripts,
        )

        for tr in transcripts:
            flagged = recache.detect(tr.turns, recache_th)
            agg.recache_cc += sum(t.cache_creation_tokens for t in flagged)
            agg.tool_result_tokens += sum(tr.tool_result_chars.values()) // _CHARS_PER_TOKEN_APPROX
            for turn in _priced_turns(tr.turns):
                resolved = rates.resolve_model(turn.model)
                agg.cost += price_turn(turn, resolved).total
                agg.priced_turns += 1
                agg.input_tokens += turn.input_tokens
                agg.cache_creation_tokens += turn.cache_creation_tokens
                for name in names:
                    if name not in present and _turn_attributed_to(turn, name):
                        present.add(name)
                if ("Bash" in turn.tool_names or "PowerShell" in turn.tool_names) and turn.cmd_prefix:
                    if _SHELL_SEARCH_RE.search(turn.cmd_prefix):
                        agg.shell_search_turns += 1

        agg.compactions = sum(1 for e in record.top.events if e.kind == EventKind.COMPACT_BOUNDARY)
        agg.present = frozenset(present)
        aggs.append(agg)
    return aggs


def _stratum_key(agg: _SessionAgg) -> tuple[str, str]:
    return (agg.purpose, agg.mode)


def _stratum_label(key: tuple[str, str]) -> str:
    purpose, mode = key
    return f"purpose={purpose or 'unknown'}, mode={mode or 'unknown'}"


# -- effect ---------------------------------------------------------------


@dataclass(slots=True)
class ArmMetrics:
    arm: str  # "present" | "absent"
    sessions: int = 0
    cost_per_session: float | None = None
    new_tokens_per_session: float | None = None
    cache_creation_per_turn: float | None = None
    mean_tool_result_tokens_per_turn: float | None = None
    recache_share_pct: float | None = None
    compactions_per_session: float | None = None
    turns_per_session: float | None = None


@dataclass(slots=True)
class StratumEffect:
    saver: str
    stratum: str  # "all", or "purpose=..., mode=..."
    present: ArmMetrics
    absent: ArmMetrics
    sample_ok: bool


def _arm_metrics(arm: str, group: Sequence[_SessionAgg]) -> ArmMetrics:
    sessions = len(group)
    if sessions == 0:
        return ArmMetrics(arm=arm, sessions=0)
    cost = sum(a.cost for a in group)
    new_tokens = sum(a.input_tokens + a.cache_creation_tokens for a in group)
    cache_creation_total = sum(a.cache_creation_tokens for a in group)
    turns_total = sum(a.priced_turns for a in group)
    recache_total = sum(a.recache_cc for a in group)
    compactions_total = sum(a.compactions for a in group)
    tool_result_tokens_total = sum(a.tool_result_tokens for a in group)
    return ArmMetrics(
        arm=arm,
        sessions=sessions,
        cost_per_session=cost / sessions,
        new_tokens_per_session=new_tokens / sessions,
        cache_creation_per_turn=(cache_creation_total / turns_total) if turns_total else None,
        mean_tool_result_tokens_per_turn=(tool_result_tokens_total / turns_total) if turns_total else None,
        recache_share_pct=(100.0 * recache_total / cache_creation_total) if cache_creation_total else None,
        compactions_per_session=compactions_total / sessions,
        turns_per_session=turns_total / sessions,
    )


def _compute_effect(
    candidates: Sequence[SaverCandidate],
    aggs: Sequence[_SessionAgg],
    min_sessions_per_arm: int,
) -> dict[str, list[StratumEffect]]:
    result: dict[str, list[StratumEffect]] = {}
    for candidate in candidates:
        name = candidate.name
        present_group = [a for a in aggs if name in a.present]
        absent_group = [a for a in aggs if name not in a.present]

        rows = [
            StratumEffect(
                saver=name,
                stratum="all",
                present=_arm_metrics("present", present_group),
                absent=_arm_metrics("absent", absent_group),
                sample_ok=(len(present_group) >= min_sessions_per_arm and len(absent_group) >= min_sessions_per_arm),
            )
        ]

        strata = sorted({_stratum_key(a) for a in present_group} | {_stratum_key(a) for a in absent_group})
        for key in strata:
            p_group = [a for a in present_group if _stratum_key(a) == key]
            a_group = [a for a in absent_group if _stratum_key(a) == key]
            rows.append(
                StratumEffect(
                    saver=name,
                    stratum=_stratum_label(key),
                    present=_arm_metrics("present", p_group),
                    absent=_arm_metrics("absent", a_group),
                    sample_ok=(len(p_group) >= min_sessions_per_arm and len(a_group) >= min_sessions_per_arm),
                )
            )
        result[name] = rows
    return result


# -- search substitution ---------------------------------------------------


@dataclass(slots=True)
class SearchSubstitutionArm:
    saver: str
    arm: str  # "present" | "absent"
    sessions: int
    native_calls_per_session: float | None
    saver_calls_per_session: float | None
    native_mean_result_tokens: float | None
    saver_mean_result_tokens: float | None
    native_carry_cost_usd_per_session: float | None
    saver_carry_cost_usd_per_session: float | None
    sample_ok: bool


def _carry_key_matches_saver(key: str, name: str) -> bool:
    return key == name or key.startswith(f"mcp__{name}__")


def _search_substitution_arm(
    arm: str,
    name: str,
    group: Sequence[_SessionAgg],
    rates: Pricing,
    min_sessions_per_arm: int,
) -> SearchSubstitutionArm:
    sessions = len(group)
    if sessions == 0:
        return SearchSubstitutionArm(
            saver=name,
            arm=arm,
            sessions=0,
            native_calls_per_session=None,
            saver_calls_per_session=None,
            native_mean_result_tokens=None,
            saver_mean_result_tokens=None,
            native_carry_cost_usd_per_session=None,
            saver_carry_cost_usd_per_session=None,
            sample_ok=False,
        )

    transcripts: list[TranscriptResult] = [t for a in group for t in a.transcripts]
    carry_stats = carry.compute_carry(transcripts, rates.resolve_model)

    native_rows = [r for r in carry_stats.by_tool if r.key in _NATIVE_SEARCH_TOOLS]
    saver_rows = [r for r in carry_stats.by_tool if _carry_key_matches_saver(r.key, name)]

    shell_search_calls = sum(a.shell_search_turns for a in group)
    native_grep_read_calls = sum(r.result_count for r in native_rows)
    native_calls = native_grep_read_calls + shell_search_calls
    saver_calls = sum(r.result_count for r in saver_rows)

    native_tokens_entered = sum(r.tokens_entered for r in native_rows)
    saver_tokens_entered = sum(r.tokens_entered for r in saver_rows)

    return SearchSubstitutionArm(
        saver=name,
        arm=arm,
        sessions=sessions,
        native_calls_per_session=native_calls / sessions,
        saver_calls_per_session=saver_calls / sessions,
        native_mean_result_tokens=(native_tokens_entered / native_grep_read_calls) if native_grep_read_calls else None,
        saver_mean_result_tokens=(saver_tokens_entered / saver_calls) if saver_calls else None,
        native_carry_cost_usd_per_session=sum(r.carry_cost_usd for r in native_rows) / sessions,
        saver_carry_cost_usd_per_session=sum(r.carry_cost_usd for r in saver_rows) / sessions,
        sample_ok=sessions >= min_sessions_per_arm,
    )


def _compute_search_substitution(
    candidates: Sequence[SaverCandidate],
    aggs: Sequence[_SessionAgg],
    rates: Pricing,
    min_sessions_per_arm: int,
) -> dict[str, list[SearchSubstitutionArm]]:
    out: dict[str, list[SearchSubstitutionArm]] = {}
    for candidate in candidates:
        name = candidate.name
        present_group = [a for a in aggs if name in a.present]
        absent_group = [a for a in aggs if name not in a.present]
        out[name] = [
            _search_substitution_arm("present", name, present_group, rates, min_sessions_per_arm),
            _search_substitution_arm("absent", name, absent_group, rates, min_sessions_per_arm),
        ]
    return out


# -- verdict ----------------------------------------------------------------

#: Flattened snapshot keys that are the presence signal itself -- never
#: worth reporting back as a "co-changed" key, since a present/absent
#: split necessarily differs on exactly these.
_PRESENCE_KEYS = frozenset(
    {
        "mcp_servers.names",
        "mcp_servers.enabled_mcpjson_servers",
        "mcp_servers.disabled_mcpjson_servers",
        "enabled_plugins",
    }
)


@dataclass(slots=True)
class SaverVerdict:
    saver: str
    sample_ok: bool
    sessions_present: int
    sessions_absent: int
    cost_per_session_present: float | None
    cost_per_session_absent: float | None
    gross_saving_per_session: float | None
    overhead_per_session: float | None
    net_saving_per_session: float | None
    co_changed_keys: tuple[str, ...] = ()
    verdict: str = "inconclusive"  # "keep" | "disable" | "inconclusive"


def _representative_snapshot(group: Sequence[_SessionAgg]) -> Snapshot | None:
    """The most common snapshot ``group``'s sessions actually joined to
    (ties broken by latest ``ts``) -- duplicated from ``compare.
    _representative_snapshot``'s identical logic (private there, see
    module docstring)."""
    by_ts: dict[str, list[Snapshot]] = {}
    for a in group:
        if a.joined_snapshot is None:
            continue
        by_ts.setdefault(a.joined_snapshot.ts, []).append(a.joined_snapshot)
    if not by_ts:
        return None
    best_ts = max(by_ts, key=lambda ts: (len(by_ts[ts]), ts))
    return by_ts[best_ts][0]


def _compute_verdicts(
    candidates: Sequence[SaverCandidate],
    effect: dict[str, list[StratumEffect]],
    overhead: dict[str, SaverOverhead],
    aggs: Sequence[_SessionAgg],
    th: "SaverThresholds",
) -> list[SaverVerdict]:
    verdicts: list[SaverVerdict] = []
    for candidate in candidates:
        name = candidate.name
        rows = effect.get(name) or []
        overall = next((r for r in rows if r.stratum == "all"), None)
        ov = overhead.get(name)
        if overall is None:
            verdicts.append(
                SaverVerdict(
                    saver=name,
                    sample_ok=False,
                    sessions_present=0,
                    sessions_absent=0,
                    cost_per_session_present=None,
                    cost_per_session_absent=None,
                    gross_saving_per_session=None,
                    overhead_per_session=None,
                    net_saving_per_session=None,
                )
            )
            continue

        cost_present = overall.present.cost_per_session
        cost_absent = overall.absent.cost_per_session
        gross_saving = (
            (cost_absent - cost_present) if (cost_present is not None and cost_absent is not None) else None
        )
        overhead_per_session = (
            (ov.cost_usd / overall.present.sessions) if (ov is not None and overall.present.sessions) else None
        )
        net_saving = (
            (gross_saving - overhead_per_session)
            if (gross_saving is not None and overhead_per_session is not None)
            else None
        )

        present_group = [a for a in aggs if name in a.present]
        absent_group = [a for a in aggs if name not in a.present]
        rep_present = _representative_snapshot(present_group)
        rep_absent = _representative_snapshot(absent_group)
        co_changed: tuple[str, ...] = ()
        if rep_present is not None and rep_absent is not None:
            co_changed = tuple(
                key for key in snapshots_mod.co_changed_keys(rep_present, rep_absent) if key not in _PRESENCE_KEYS
            )

        verdict_label = "inconclusive"
        if overall.sample_ok and net_saving is not None:
            if net_saving > th.net_saving_usd_min:
                verdict_label = "keep"
            elif net_saving < -th.net_saving_usd_min:
                verdict_label = "disable"

        verdicts.append(
            SaverVerdict(
                saver=name,
                sample_ok=overall.sample_ok,
                sessions_present=overall.present.sessions,
                sessions_absent=overall.absent.sessions,
                cost_per_session_present=cost_present,
                cost_per_session_absent=cost_absent,
                gross_saving_per_session=gross_saving,
                overhead_per_session=overhead_per_session,
                net_saving_per_session=net_saving,
                co_changed_keys=co_changed,
                verdict=verdict_label,
            )
        )
    return verdicts


# -- thresholds --------------------------------------------------------------


@dataclass(slots=True)
class SaverThresholds:
    """Every tunable number this module's own logic depends on.
    :meth:`from_config` reads ``config.toml``'s ``[thresholds.savers]``
    table when a caller uses it (nothing does yet; see the module
    docstring) (mirrors ``RecommendThresholds``/``ScorecardThresholds``'s own
    per-module-subtable convention, rather than ``RecacheThresholds``/
    ``LimitThresholds``'s shared flat ``[thresholds]`` namespace -- see
    :meth:`from_config`).
    """

    #: A present/absent arm needs at least this many sessions before its
    #: effect/verdict figures are shown as sample_ok.
    min_sessions_per_arm: int = 5
    #: A keep verdict additionally suggests limiting result size when
    #: the saver's own overhead exceeds this share of its gross saving.
    overhead_share_pct: float = 20.0
    #: The net-saving-per-session magnitude (either direction) a
    #: keep/disable recommendation requires.
    net_saving_usd_min: float = 0.01
    #: Explicit names from ``config.toml``'s ``[savers] names`` list --
    #: see the module docstring's deviation note on why this lives here
    #: rather than a ``config`` parameter on ``compute_saver_roi``.
    configured_names: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, data: dict | None, config: "Config | None" = None) -> "SaverThresholds":
        """Build thresholds from ``config.toml``'s ``[thresholds.savers]``
        table (``data``) plus ``config`` itself for ``min_sessions``
        (default) and the explicit ``[savers] names`` allowlist. Any
        absent or malformed key keeps this class's default; unknown keys
        are ignored -- same posture as ``RecacheThresholds.from_config``/
        ``RecommendThresholds.from_config``.
        """
        base_kwargs: dict = {}
        if config is not None:
            base_kwargs["min_sessions_per_arm"] = config.min_sessions
            base_kwargs["configured_names"] = tuple(str(n) for n in (getattr(config, "savers", None) or ()))
        defaults = cls(**base_kwargs)
        if not isinstance(data, dict):
            return defaults
        kwargs: dict = {}
        for f in ("min_sessions_per_arm", "overhead_share_pct", "net_saving_usd_min"):
            if f in data:
                default_value = getattr(defaults, f)
                try:
                    kwargs[f] = type(default_value)(data[f])
                except (TypeError, ValueError):
                    continue
        if not kwargs:
            return defaults
        return cls(**{**{f: getattr(defaults, f) for f in defaults.__dataclass_fields__}, **kwargs})

    def describe(self) -> list[str]:
        return [
            f"min_sessions_per_arm = {self.min_sessions_per_arm}: a present/absent arm below this many "
            "sessions has every effect/verdict figure suppressed (sample_ok=no).",
            f"overhead_share_pct = {self.overhead_share_pct:.1f}%: a keep verdict additionally suggests "
            "limiting result size when the saver's own overhead exceeds this share of its gross saving.",
            f"net_saving_usd_min = {self.net_saving_usd_min:.2f} USD: the net-saving-per-session magnitude "
            "(either direction) a keep/disable recommendation requires.",
        ]


_DEFAULT_THRESHOLDS = SaverThresholds()
_DEFAULT_RECACHE_THRESHOLDS = recache.RecacheThresholds()


# -- top-level stats + entry point -------------------------------------------


@dataclass(slots=True)
class SaverStats:
    """Everything :func:`compute_saver_roi` computes, ready for
    :func:`build_section` to render."""

    candidates: list[SaverCandidate]
    overhead: dict[str, SaverOverhead]
    effect: dict[str, list[StratumEffect]]
    search_substitution: dict[str, list[SearchSubstitutionArm]]
    verdicts: list[SaverVerdict]


def compute_saver_roi(
    results: Sequence[TranscriptResult],
    sessions: Sequence[SessionRecord],
    snapshots: Sequence[Snapshot],
    rates: Pricing,
    thresholds: SaverThresholds | None = None,
) -> SaverStats:
    """Compute every saver-ROI metric the module docstring describes.

    ``results`` is every transcript (top-level and subagent) in the
    corpus, flat -- the same shape ``detect_savers``/``carry.
    compute_carry``/``topology.TopologyStats`` take -- used for
    detection and overhead (both corpus-wide, turn-level sums).
    ``sessions`` is the corpus's already-built, already-classified
    ``SessionRecord``\\ s (``classify.build_session_record``'s own
    output) -- used for the per-session present/absent split, since a
    ``SessionRecord`` already carries its own classification and
    ``top``/``subs`` transcripts. ``snapshots`` joins each session to its
    config at the time it started (``snapshots.snapshot_for``).
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    candidates = _detect(results, snapshots, th.configured_names)
    names = [c.name for c in candidates]

    overhead = _compute_overhead(candidates, results, rates)
    aggs = _build_session_aggs(sessions, snapshots, names, rates, _DEFAULT_RECACHE_THRESHOLDS)
    effect = _compute_effect(candidates, aggs, th.min_sessions_per_arm)
    search_substitution = _compute_search_substitution(candidates, aggs, rates, th.min_sessions_per_arm)
    verdicts = _compute_verdicts(candidates, effect, overhead, aggs, th)

    return SaverStats(
        candidates=candidates,
        overhead=overhead,
        effect=effect,
        search_substitution=search_substitution,
        verdicts=verdicts,
    )


# -- report section -----------------------------------------------------------


def _build_detected_table(stats: SaverStats) -> Table:
    columns = [
        Column(key="saver", label="Saver", kind="str"),
        Column(key="from_config", label="From config.toml [savers]", kind="str"),
        Column(key="detected_via", label="Detected via", kind="str"),
    ]
    rows = [
        [c.name, "yes" if c.from_config else "no", ", ".join(c.detected_via) or "(config only)"]
        for c in stats.candidates
    ]
    return Table(name="savers_detected", title="Detected token-saver tools", columns=columns, rows=rows)


def _build_overhead_table(stats: SaverStats) -> Table:
    columns = [
        Column(key="saver", label="Saver", kind="str"),
        Column(key="turns", label="Attributed turns", kind="int"),
        Column(key="cost_usd", label="Attributed cost", kind="money"),
        Column(key="calls", label="Tool calls", kind="int"),
        Column(key="result_chars", label="Tool-result chars", kind="int"),
        Column(key="mean_result_chars", label="Mean result size (chars)", kind="float"),
        Column(key="distinct_tool_names", label="Distinct mcp__* tool names (schema proxy)", kind="int"),
    ]
    rows = []
    for c in stats.candidates:
        ov = stats.overhead.get(c.name)
        if ov is None:
            continue
        rows.append([ov.name, ov.turns, ov.cost_usd, ov.calls, ov.result_chars, ov.mean_result_chars, ov.distinct_tool_names])
    return Table(
        name="savers_overhead",
        title="Saver overhead",
        columns=columns,
        rows=rows,
        notes=["Schema/prefix-load bytes are not observable anywhere in this codebase; distinct tool-name count is the closest proxy."],
    )


def _stratum_row_key(saver: str, stratum: str) -> str:
    """A unique row key for one (saver, stratum) row: ``saver`` alone
    repeats across every stratum of the same saver, so column 0 (the
    row-key column every evidence lookup in this codebase indexes by --
    see ``carry._row``/``recommend._row``) has to be this pair, not
    ``saver`` alone."""
    return f"{saver} | {stratum}"


def _build_effect_table(stats: SaverStats) -> Table:
    columns = [
        Column(key="row_key", label="Saver | Stratum", kind="str"),
        Column(key="saver", label="Saver", kind="str"),
        Column(key="stratum", label="Stratum", kind="str"),
        Column(key="sessions_present", label="Sessions (present)", kind="int"),
        Column(key="sessions_absent", label="Sessions (absent)", kind="int"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
        Column(key="cost_present", label="Cost/session (present)", kind="money"),
        Column(key="cost_absent", label="Cost/session (absent)", kind="money"),
        Column(key="new_tokens_present", label="New tokens/session (present)", kind="tokens"),
        Column(key="new_tokens_absent", label="New tokens/session (absent)", kind="tokens"),
        Column(key="cache_creation_per_turn_present", label="Cache-creation/turn (present)", kind="tokens"),
        Column(key="cache_creation_per_turn_absent", label="Cache-creation/turn (absent)", kind="tokens"),
        Column(key="tool_result_tokens_per_turn_present", label="Tool-result tokens/turn (present, approx)", kind="tokens"),
        Column(key="tool_result_tokens_per_turn_absent", label="Tool-result tokens/turn (absent, approx)", kind="tokens"),
        Column(key="recache_share_present", label="Re-cache share (present)", kind="pct"),
        Column(key="recache_share_absent", label="Re-cache share (absent)", kind="pct"),
        Column(key="compactions_present", label="Compactions/session (present)", kind="float"),
        Column(key="compactions_absent", label="Compactions/session (absent)", kind="float"),
        Column(key="turns_present", label="Turns/session (present)", kind="float"),
        Column(key="turns_absent", label="Turns/session (absent)", kind="float"),
    ]
    rows = []
    for c in stats.candidates:
        for row in stats.effect.get(c.name, []):
            p, a = row.present, row.absent
            rows.append(
                [
                    _stratum_row_key(row.saver, row.stratum),
                    row.saver,
                    row.stratum,
                    p.sessions,
                    a.sessions,
                    "yes" if row.sample_ok else "no",
                    p.cost_per_session,
                    a.cost_per_session,
                    p.new_tokens_per_session,
                    a.new_tokens_per_session,
                    p.cache_creation_per_turn,
                    a.cache_creation_per_turn,
                    p.mean_tool_result_tokens_per_turn,
                    a.mean_tool_result_tokens_per_turn,
                    p.recache_share_pct,
                    a.recache_share_pct,
                    p.compactions_per_session,
                    a.compactions_per_session,
                    p.turns_per_session,
                    a.turns_per_session,
                ]
            )
    return Table(
        name="savers_effect_by_stratum",
        title="Effect: sessions with the saver present vs absent",
        columns=columns,
        rows=rows,
        notes=["Observed, not controlled: present/absent sessions also differ in workload (see module assumptions)."],
    )


def _substitution_row_key(saver: str, arm: str) -> str:
    """A unique row key for one (saver, arm) row -- see
    :func:`_stratum_row_key`'s identical rationale: ``saver`` alone
    repeats across its present/absent rows."""
    return f"{saver} | {arm}"


def _build_search_substitution_table(stats: SaverStats) -> Table:
    columns = [
        Column(key="row_key", label="Saver | Arm", kind="str"),
        Column(key="saver", label="Saver", kind="str"),
        Column(key="arm", label="Arm", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="native_calls_per_session", label="Native search calls/session (Grep/Glob/Read/shell)", kind="float"),
        Column(key="saver_calls_per_session", label="Saver tool calls/session", kind="float"),
        Column(key="native_mean_result_tokens", label="Mean native result size (tokens, approx)", kind="tokens"),
        Column(key="saver_mean_result_tokens", label="Mean saver result size (tokens, approx)", kind="tokens"),
        Column(key="native_carry_cost_per_session", label="Native carry cost/session", kind="money"),
        Column(key="saver_carry_cost_per_session", label="Saver carry cost/session", kind="money"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
    ]
    rows = []
    for c in stats.candidates:
        for arm in stats.search_substitution.get(c.name, []):
            rows.append(
                [
                    _substitution_row_key(arm.saver, arm.arm),
                    arm.saver,
                    arm.arm,
                    arm.sessions,
                    arm.native_calls_per_session,
                    arm.saver_calls_per_session,
                    arm.native_mean_result_tokens,
                    arm.saver_mean_result_tokens,
                    arm.native_carry_cost_usd_per_session,
                    arm.saver_carry_cost_usd_per_session,
                    "yes" if arm.sample_ok else "no",
                ]
            )
    return Table(
        name="savers_search_substitution",
        title="Search substitution: native search vs the saver's own tools",
        columns=columns,
        rows=rows,
        notes=[
            "Carry-cost columns reuse carry.compute_carry over each (saver, arm)'s own transcripts.",
            "Grep/Glob/Read calls are counted per (turn, tool) pair; a Bash/PowerShell search call is "
            "counted once per turn (only the turn's first shell command is retained) -- see module assumptions.",
        ],
    )


def _build_verdict_table(stats: SaverStats) -> Table:
    columns = [
        Column(key="saver", label="Saver", kind="str"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
        Column(key="sessions_present", label="Sessions (present)", kind="int"),
        Column(key="sessions_absent", label="Sessions (absent)", kind="int"),
        Column(key="cost_present", label="Cost/session (present)", kind="money"),
        Column(key="cost_absent", label="Cost/session (absent)", kind="money"),
        Column(key="gross_saving", label="Gross saving/session (absent - present)", kind="money"),
        Column(key="overhead", label="Overhead/session", kind="money"),
        Column(key="net_saving", label="Net saving/session", kind="money"),
        Column(key="co_changed_keys", label="Other co-changed config keys", kind="str"),
        Column(key="verdict", label="Verdict", kind="str"),
    ]
    rows = [
        [
            v.saver,
            "yes" if v.sample_ok else "no",
            v.sessions_present,
            v.sessions_absent,
            v.cost_per_session_present,
            v.cost_per_session_absent,
            v.gross_saving_per_session,
            v.overhead_per_session,
            v.net_saving_per_session,
            ", ".join(v.co_changed_keys) or "(none)",
            v.verdict,
        ]
        for v in stats.verdicts
    ]
    return Table(
        name="savers_verdict",
        title="Verdict: net saving per session",
        columns=columns,
        rows=rows,
        notes=[
            "net saving/session = (cost/session absent - cost/session present) - overhead/session. "
            "Observed, not controlled -- see module assumptions."
        ],
    )


def build_section(stats: SaverStats, thresholds: SaverThresholds | None = None) -> Section:
    """Render ``stats`` into the ``savers`` report section: detection,
    overhead, effect-by-stratum, search-substitution, and verdict.

    Still renders (with empty tables) when ``stats.candidates`` is
    empty, with a note telling the reader how to name their own saver
    explicitly.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    tables = [
        _build_detected_table(stats),
        _build_overhead_table(stats),
        _build_effect_table(stats),
        _build_search_substitution_table(stats),
        _build_verdict_table(stats),
    ]
    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if not stats.candidates:
        notes.append(
            "No token-saver tool detected in this window. If you run one (an MCP server, plugin, or "
            "skill claiming to save tokens), name it explicitly in config.toml's [savers] table, e.g. "
            '[savers]\\nnames = ["my-mcp-server"] -- see the Config section for the MCP server and '
            "plugin names this tool already knows about, or run `claudeglass probe-config`."
        )
    return Section(key="savers", title="Third-party token-saver tool ROI", tables=tables, notes=notes)


# -- recommendation rule -------------------------------------------------


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in report.sections:
        if section.key == section_key:
            for table in section.tables:
                if table.name == table_name:
                    return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for idx, column in enumerate(table.columns):
        if column.key == column_key:
            return idx
    return None


def _cell(table: Table, row, column_key: str):
    idx = _col_index(table, column_key)
    if idx is None:
        return None
    for r in table.rows:
        if r and r[0] == row[0]:
            if idx >= len(r):
                return None
            return r[idx]
    return None


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def _lever_and_scope(name: str, snapshot: Snapshot | None) -> tuple[str, str]:
    """Always ``mcpServers.<name>``, even for a candidate only ever seen
    as a plugin or a skill (the module docstring notes this is not a key
    the report's fixes can change). Scope is "user" by default, upgraded
    to "managed" when the underlying top-level key
    (``mcpServers``/``enabledPlugins``) appears in
    ``snapshots.managed_keys(snapshot)`` (same convention as
    ``recommend._lever_scope``/``model_swap._scope_for``)."""
    lever = f"mcpServers.{name}"
    scope = "user"
    if snapshot is not None:
        keys = set(managed_keys(snapshot))
        if "mcpServers" in keys or "enabledPlugins" in keys:
            scope = "managed"
    return lever, scope


def _action_with_scope(action: str, scope: str) -> str:
    if scope == "managed":
        return f"{action} This lever is managed by policy, raise with your administrator."
    return action


def _rule_saver_tool_roi(
    report: ReportModel,
    th: SaverThresholds,
    snapshot: Snapshot | None = None,
) -> list[Recommendation]:
    """``saver-tool-roi``: fires once per saver whose ``savers_verdict``
    row clears the minimum sample and whose net saving/session clears
    either side of ``th.net_saving_usd_min`` -- see the module docstring.
    """
    verdict_table = _table(report, "savers", "savers_verdict")
    substitution_table = _table(report, "savers", "savers_search_substitution")
    if verdict_table is None:
        return []

    net_idx = _col_index(verdict_table, "net_saving")
    sample_idx = _col_index(verdict_table, "sample_ok")
    gross_idx = _col_index(verdict_table, "gross_saving")
    overhead_idx = _col_index(verdict_table, "overhead")
    if net_idx is None or sample_idx is None:
        return []

    # UX-2: units may be unset (a caller without a billing config) --
    # money_text still gives a plain currency-suffixed number rather than
    # a bare "$" in that case.
    units = report.units

    out: list[Recommendation] = []
    for row in verdict_table.rows:
        name = row[0]
        if row[sample_idx] != "yes":
            continue
        net_saving = row[net_idx]
        if not isinstance(net_saving, (int, float)):
            continue

        lever, scope = _lever_and_scope(name, snapshot)
        net_saving_text = units.money_text(net_saving) if units is not None else f"${net_saving:.4f}"

        if net_saving > th.net_saving_usd_min:
            evidence = [_evidence("Net saving per session", net_saving, "savers", "savers_verdict", name)]
            gross = row[gross_idx] if gross_idx is not None else None
            overhead = row[overhead_idx] if overhead_idx is not None else None
            action = f"{name} correlates with a net saving of {net_saving_text} per session -- keep it enabled."
            if isinstance(gross, (int, float)) and isinstance(overhead, (int, float)) and gross > 0:
                evidence.append(_evidence("Gross saving per session", gross, "savers", "savers_verdict", name))
                evidence.append(_evidence("Overhead per session", overhead, "savers", "savers_verdict", name))
                if overhead > (th.overhead_share_pct / 100.0) * gross:
                    action += (
                        f" Its own overhead is more than {th.overhead_share_pct:.0f}% of its gross saving -- "
                        "ask it (or its config) to return smaller results if that's configurable."
                    )
            if substitution_table is not None:
                p_key = _substitution_row_key(name, "present")
                a_key = _substitution_row_key(name, "absent")
                p_row = next((r for r in substitution_table.rows if r[0] == p_key), None)
                a_row = next((r for r in substitution_table.rows if r[0] == a_key), None)
                if p_row is not None and a_row is not None:
                    native_idx = _col_index(substitution_table, "native_calls_per_session")
                    native_size_idx = _col_index(substitution_table, "native_mean_result_tokens")
                    saver_size_idx = _col_index(substitution_table, "saver_mean_result_tokens")
                    if native_idx is not None and p_row[native_idx] is not None and a_row[native_idx] is not None:
                        if p_row[native_idx] < a_row[native_idx]:
                            evidence.append(
                                _evidence(
                                    "Native search calls/session (present)", p_row[native_idx],
                                    "savers", "savers_search_substitution", p_key,
                                )
                            )
                            evidence.append(
                                _evidence(
                                    "Native search calls/session (absent)", a_row[native_idx],
                                    "savers", "savers_search_substitution", a_key,
                                )
                            )
                            action += " It also displaces native Grep/Read/Glob/shell search calls."
                    if (
                        native_size_idx is not None
                        and saver_size_idx is not None
                        and isinstance(p_row[saver_size_idx], (int, float))
                        and isinstance(a_row[native_size_idx], (int, float))
                        and p_row[saver_size_idx] < a_row[native_size_idx]
                    ):
                        evidence.append(
                            _evidence(
                                "Saver mean result size (tokens)", p_row[saver_size_idx],
                                "savers", "savers_search_substitution", p_key,
                            )
                        )
                        evidence.append(
                            _evidence(
                                "Native mean result size (tokens)", a_row[native_size_idx],
                                "savers", "savers_search_substitution", a_key,
                            )
                        )
                        action += " Its own results also run smaller than the native search calls it replaces."
            out.append(
                Recommendation(
                    id="saver-tool-roi",
                    severity="advice",
                    category="settings",
                    title=f"{name} is paying for itself",
                    action=_action_with_scope(action, scope),
                    lever=lever,
                    scope=scope,
                    evidence=evidence,
                )
            )
        elif net_saving < -th.net_saving_usd_min:
            evidence = [_evidence("Net saving per session", net_saving, "savers", "savers_verdict", name)]
            cost_text = units.money_text(-net_saving) if units is not None else f"${-net_saving:.4f}"
            # UX-2: the second mention hedges with "approximately" -- but
            # cost_text already opens with "about" under a subscription
            # (units.Units.money's own weekly-limit-share phrasing), so
            # "approximately" is skipped there rather than reading
            # "approximately about X% of your weekly usage limit..." (the
            # same doubling finding F3 covers for a literal "about about").
            approx_cost_text = (
                cost_text if cost_text.lower().startswith("about ") else f"approximately {cost_text}"
            )
            action = (
                f"{name} correlates with a net cost of {cost_text} per session once its own overhead is "
                f"counted -- disabling it projects a saving of {approx_cost_text} per session at "
                "today's volumes."
            )
            out.append(
                Recommendation(
                    id="saver-tool-roi",
                    severity="advice",
                    category="settings",
                    title=f"{name} is not paying for itself",
                    action=_action_with_scope(action, scope),
                    lever=lever,
                    scope=scope,
                    evidence=evidence,
                )
            )
    return out


#: One rule, keyed by id -- a wiring agent folds this into
#: ``recommend.py``'s own rule dispatch via
#: ``RULES["saver-tool-roi"](report, th, snapshot)`` (same
#: ``model_swap.RULES``-dict convention; this module doesn't import or
#: modify ``recommend.py`` -- see the module docstring).
RULES: dict[str, Callable[..., list[Recommendation]]] = {
    "saver-tool-roi": _rule_saver_tool_roi,
}


__all__ = [
    "ASSUMPTIONS",
    "SaverCandidate",
    "detect_savers",
    "SaverOverhead",
    "ArmMetrics",
    "StratumEffect",
    "SearchSubstitutionArm",
    "SaverVerdict",
    "SaverThresholds",
    "SaverStats",
    "compute_saver_roi",
    "build_section",
    "RULES",
]

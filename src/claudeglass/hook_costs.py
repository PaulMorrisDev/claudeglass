"""Your own hooks (``hooks``): whether each hook you set up works, and what
it costs in kept context, blocked calls and waiting.

Read from what ``parse.py`` keeps per hook (see model.py's "Your-hooks
addition"): each hook run's label (its script's file name), why a failed
run failed, whether its script path is relative, the context a hook added
before each reply, and the tool calls a hook blocked (and whether Claude
then sent the same call again unchanged).

The model, per hook label:

- **Failed runs.** ``hook_non_blocking_error`` records, with the sessions
  they fell in, when the last one happened, the most common cause and
  whether the command names its script by a relative path (which only
  resolves when Claude Code runs the hook from the project root) or
  through a Windows ``%VAR%`` variable (which the hook's shell leaves as
  it is).
- **Stopped failing.** A hook fixed mid-window keeps its old failures
  until they age out, so each failing hook is judged on what came after
  its last failure, in the projects it failed in: the calls to the tools
  it failed on (a tool hook's clean runs leave no record), or its own
  recorded runs (other events record their clean runs). At the rate it
  used to fail, if at least ``quiet_failures`` more failures should have
  followed and none did, it has stopped failing.
- **Runs seen working.** Successful runs Claude Code recorded, plus calls
  and stops the hook blocked. A tool hook that ran cleanly and let the
  call through leaves no record, so this undercounts those.
- **Cost of blocks.** A blocked call costs the next reply, which reads the
  block: that reply's price times the blocked calls' share of the calls
  it answered. **Sent again unchanged**: calls Claude re-sent with the
  same input after the block, priced at the same share of the block cost.
- **Cost of its context.** Context a hook adds is kept like a tool output
  (``carry.py``'s model): its tokens times each later reply's carry rate,
  from the reply it lands before until the next summary or the end.
- **Time waited.** The hook's recorded run time (``durationMs``).

Context Claude Code adds itself (``built-in``) is left out: you can't
change it. ClaudeGlass's own capture note is folded into its hook's row
from ``Turn.cap_note_chars``; the capture section prices it too.

Same shape as ``carry.py``: :class:`HookThresholds`,
:func:`compute_hook_costs`, :func:`build_section` and :data:`RULES`
(folded into ``recommend.recommend``).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .capture_catalogue import HOOK_SCRIPT
from .carry import boundary_turn_indices, carry_end_index, carry_rate_prefix
from .model import Column, EventKind, Recommendation, ReportModel, Section, Table, TranscriptResult
from .parse import BUILT_IN_TOOLS, tool_server
from .pricing import Pricing, price_turn

#: Characters per token, the approximation used throughout this report
#: (duplicated per module by convention, see ``carry.py``).
_CHARS_PER_TOKEN_APPROX = 4

#: The label ``parse.py`` gives context Claude Code added itself.
BUILT_IN = "built-in"

#: Hook records that are a run of one of your hooks.
_RUN_SUBKINDS = frozenset({"hook_success", "hook_non_blocking_error", "hook_blocking_error", "hook_cancelled"})

#: A failed run's cause, as the table words it.
_CAUSE_WORDS = {"not-found": "script not found", "timeout": "timed out", "failed": "error"}

ASSUMPTIONS: tuple[str, ...] = (
    "context a hook adds is kept like a tool output: read again on every later reply until a summary or "
    "the end of the transcript",
    "a call a hook blocked costs the next reply, which reads the block. When that reply reads several tool "
    "results, the block takes its share",
    "Claude Code records a tool hook's run only when it fails, blocks or adds context, so a hook's clean "
    "runs are undercounted",
)


@dataclass(slots=True)
class HookThresholds:
    """Every tunable number the hook checks depend on (same
    ``from_config``/``describe`` convention as ``carry.CarryThresholds``).
    Config keys carry a ``hooks_`` prefix: ``[thresholds]`` is one flat
    table shared by every module."""

    #: The failing-hooks card needs a hook with at least this many failed
    #: runs.
    min_failures: int = 10
    #: The blocked-calls card needs a hook that blocked at least this many
    #: calls...
    min_blocks: int = 10
    #: ...at least this share (%) of which Claude sent again unchanged.
    resend_share_pct: float = 50.0
    #: The hook-context card needs a hook that added context at least this
    #: many times...
    min_contexts: int = 20
    #: ...and whose context cost at least this much to keep (USD, list
    #: price).
    min_context_usd: float = 1.0
    #: A failing hook has stopped failing when, at the rate it used to
    #: fail, at least this many more failures should have followed its
    #: last one (none did: it was the last).
    quiet_failures: float = 5.0
    #: How many hooks ``hooks_by_script`` lists.
    top_n: int = 20

    @classmethod
    def from_config(cls, config: dict | None) -> "HookThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        for name, cast in (
            ("min_failures", int),
            ("min_blocks", int),
            ("resend_share_pct", float),
            ("min_contexts", int),
            ("min_context_usd", float),
            ("quiet_failures", float),
            ("top_n", int),
        ):
            key = f"hooks_{name}"
            if key in data:
                try:
                    kwargs[name] = cast(data[key])
                except (TypeError, ValueError):
                    pass
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [
            f"The failing-hooks tip needs a hook with at least {self.min_failures} failed runs.",
            f"The blocked-calls tip needs a hook that blocked at least {self.min_blocks} calls, "
            f"{self.resend_share_pct:.0f}% or more of them sent again unchanged.",
            f"The hook-context tip needs a hook that added context at least {self.min_contexts} times, "
            f"costing at least ${self.min_context_usd:,.2f} to keep.",
            f"A hook has stopped failing when, at the rate it used to fail, {self.quiet_failures:g} or more "
            "failures should have followed its last one.",
            f"The hooks table lists the top {self.top_n}.",
        ]


_DEFAULT_THRESHOLDS = HookThresholds()


@dataclass(slots=True)
class HookRow:
    """One hook, by label: counts and costs only."""

    label: str
    events: set[str] = field(default_factory=set)
    failed: int = 0
    causes: dict[str, int] = field(default_factory=dict)
    relative: bool = False
    unexpanded: bool = False
    failed_sessions: set[str] = field(default_factory=set)
    #: The latest failure's timestamp, as the transcript wrote it.
    last_failed_ts: str = ""
    failed_projects: set[str] = field(default_factory=set)
    #: The tools its failures ran for (``mcp`` for any MCP tool); see
    #: :attr:`tool_less` for a hook whose failures name none (``Stop``,
    #: ``SessionStart``...).
    failed_tools: set[str] = field(default_factory=set)
    #: The hook events of the failures that name no tool.
    failed_events: set[str] = field(default_factory=set)
    #: What it could have failed on in its projects, up to its last
    #: failure and after it (see :func:`_since`).
    chances_before: int = 0
    chances_since: int = 0
    worked: int = 0
    blocks: int = 0
    stop_blocks: int = 0
    resent: int = 0
    block_usd: float = 0.0
    contexts: int = 0
    context_tokens: int = 0
    carry_usd: float = 0.0
    wait_ms: float = 0.0
    failed_wait_ms: float = 0.0

    @property
    def cause(self) -> str:
        """The most common cause of a failed run, or ``""``."""
        if not self.causes:
            return ""
        return max(sorted(self.causes), key=lambda c: self.causes[c])

    @property
    def last_failed(self) -> str:
        """The latest failure as ``2026-09-28 07:38 UTC``, or ``""``."""
        return _utc_minute(self.last_failed_ts)

    def stopped(self, thresholds: HookThresholds | None = None) -> bool:
        """Whether it has stopped failing: see :attr:`HookThresholds.quiet_failures`."""
        return bool(self.failed) and self.expected_since >= (thresholds or _DEFAULT_THRESHOLDS).quiet_failures

    @property
    def tool_less(self) -> bool:
        """Whether none of its failures named a tool. One that did makes
        it a tool hook even if others didn't: those come from a
        transcript read before PARSER_VERSION 32 whose file is gone, so
        the store can't read it again."""
        return not self.failed_tools

    @property
    def expected_since(self) -> float:
        """The failures that should have followed its last one, at the
        rate it failed up to then."""
        if not self.failed or not self.chances_since:
            return 0.0
        return self.failed / max(self.chances_before, self.failed) * self.chances_since

    @property
    def resent_usd(self) -> float:
        """The share of the block cost spent on calls sent again unchanged."""
        return self.block_usd * min(1.0, self.resent / self.blocks) if self.blocks else 0.0


@dataclass(slots=True)
class HookStats:
    hooks: dict[str, HookRow] = field(default_factory=dict)
    #: Context Claude Code added itself (left out of ``hooks``).
    built_in_tokens: int = 0
    built_in_usd: float = 0.0

    def row(self, label: str) -> HookRow:
        found = self.hooks.get(label)
        if found is None:
            found = self.hooks[label] = HookRow(label=label)
        return found

    def ranked(self) -> list[HookRow]:
        """Hooks, costliest first: failures, then money, then waiting."""
        return sorted(
            self.hooks.values(),
            key=lambda h: (-h.failed, -(h.block_usd + h.carry_usd), -h.wait_ms, h.label),
        )


def _utc_minute(ts: str) -> str:
    try:
        at = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return ts[:10]
    if at is None:
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _events(tr: TranscriptResult, stats: HookStats, project: str) -> None:
    for event in tr.events:
        if event.kind != EventKind.HOOK_OUTPUT or event.subkind not in _RUN_SUBKINDS:
            continue
        label = event.detail.get("script")
        if not isinstance(label, str) or not label:
            continue
        row = stats.row(label)
        bucket = event.detail.get("hookName")
        if isinstance(bucket, str):
            row.events.add(bucket)
        duration = event.detail.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            row.wait_ms += duration
        if event.detail.get("relative"):
            row.relative = True
        if event.detail.get("unexpanded"):
            row.unexpanded = True
        if event.subkind == "hook_non_blocking_error":
            row.failed += 1
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                row.failed_wait_ms += duration
            cause = event.detail.get("cause")
            cause = cause if isinstance(cause, str) else "failed"
            row.causes[cause] = row.causes.get(cause, 0) + 1
            if tr.meta.session_id:
                row.failed_sessions.add(tr.meta.session_id)
            row.failed_projects.add(project)
            if (event.ts or "") > row.last_failed_ts:
                row.last_failed_ts = event.ts or ""
            tool = event.detail.get("tool")
            if isinstance(tool, str) and tool:
                row.failed_tools.add(tool)
            else:
                row.failed_events.add(bucket if isinstance(bucket, str) else "other")
        elif event.subkind == "hook_blocking_error":
            row.stop_blocks += 1
            row.worked += 1
        elif event.subkind == "hook_success":
            row.worked += 1


def _turns(tr: TranscriptResult, lookup, stats: HookStats) -> None:
    priced = [t for t in tr.turns if t.turn_index > 0]
    if not priced:
        return
    has_context = any(t.hook_context_chars or t.cap_note_chars for t in priced)
    if has_context:
        boundaries = boundary_turn_indices(priced)
        last_index = priced[-1].turn_index
        turn_indices = [t.turn_index for t in priced]
        prefix_rate = carry_rate_prefix(priced, lookup)
    for k, turn in enumerate(priced):
        if turn.hook_blocks:
            calls = sum(turn.tool_calls_by_tool.values()) or 1
            next_cost = price_turn(priced[k + 1], lookup(priced[k + 1].model)).total if k + 1 < len(priced) else 0.0
            for label, blocked in turn.hook_blocks.items():
                row = stats.row(label)
                row.blocks += blocked
                row.worked += blocked
                row.block_usd += next_cost * min(1.0, blocked / calls)
        for label, resent in turn.hook_resends.items():
            stats.row(label).resent += resent
        if not has_context:
            continue
        added = dict(turn.hook_context_chars)
        if turn.cap_note_chars:
            added[HOOK_SCRIPT] = added.get(HOOK_SCRIPT, 0) + turn.cap_note_chars
        if not added:
            continue
        end_index = carry_end_index(turn.turn_index, boundaries, last_index)
        right = bisect_right(turn_indices, end_index)
        # From this reply (the context lands in front of it) through the
        # last one before the next summary.
        rate_sum = prefix_rate[right] - prefix_rate[k] if right > k else 0.0
        for label, chars in added.items():
            tokens = round(chars / _CHARS_PER_TOKEN_APPROX)
            if tokens <= 0:
                continue
            if label == BUILT_IN:
                stats.built_in_tokens += tokens
                stats.built_in_usd += tokens * rate_sum
                continue
            row = stats.row(label)
            row.contexts += 1
            row.context_tokens += tokens
            row.carry_usd += tokens * rate_sum


def _since(tr: TranscriptResult, stats: HookStats, project: str) -> None:
    """Count each failing hook's chances to fail before and after its
    last failure, in the projects it failed in. A tool hook's clean runs
    leave no record, so its chances are the calls to the tools it failed
    on. Neither does a SessionStart hook's, so its chances are the
    sessions that start and the summaries (each starts the session
    again). Any other hook's are its own recorded runs."""
    rows = [
        h for h in stats.hooks.values()
        if h.failed and h.last_failed_ts and project in h.failed_projects
    ]
    if not rows:
        return
    for row in rows:
        if row.tool_less:
            continue
        for turn in tr.turns:
            calls = sum(
                n for name, n in turn.tool_calls_by_tool.items()
                if name in row.failed_tools or ("mcp" in row.failed_tools and tool_server(name) != BUILT_IN_TOOLS)
            )
            if not calls or not turn.ts:
                continue
            if turn.ts > row.last_failed_ts:
                row.chances_since += calls
            else:
                row.chances_before += calls
    starts = [h for h in rows if h.tool_less and h.failed_events == {"SessionStart"}]
    if starts:
        times = [e.ts for e in tr.events if e.kind == EventKind.COMPACT_BOUNDARY and e.ts]
        if tr.meta.kind == "top-level":
            # Its earliest record: the start, as its own SessionStart
            # hooks ran, comes before the first reply.
            times.append(min([t.ts for t in tr.turns if t.ts] + [e.ts for e in tr.events if e.ts], default=""))
        for row in starts:
            for ts in times:
                if not ts:
                    continue
                if ts > row.last_failed_ts:
                    row.chances_since += 1
                else:
                    row.chances_before += 1
    by_label = {h.label: h for h in rows if h.tool_less and h.failed_events != {"SessionStart"}}
    if not by_label:
        return
    for event in tr.events:
        if event.kind != EventKind.HOOK_OUTPUT or event.subkind not in _RUN_SUBKINDS or not event.ts:
            continue
        row = by_label.get(event.detail.get("script"))
        if row is None:
            continue
        if event.ts > row.last_failed_ts:
            row.chances_since += 1
        else:
            row.chances_before += 1


def failure_stats(results: list[TranscriptResult]) -> HookStats:
    """Each hook's runs, failures and what came after its last failure,
    with nothing priced: all :meth:`HookRow.stopped` needs. ``capture
    status``'s failing-hook warning reads it too."""
    stats = HookStats()
    # A subagent's transcript names no project: its session's does.
    projects = {tr.meta.session_id: tr.meta.project_slug for tr in results if tr.meta.project_slug}
    for tr in results:
        _events(tr, stats, tr.meta.project_slug or projects.get(tr.meta.session_id, ""))
    for tr in results:
        _since(tr, stats, tr.meta.project_slug or projects.get(tr.meta.session_id, ""))
    return stats


def compute_hook_costs(
    results: list[TranscriptResult], pricing: Pricing, thresholds: HookThresholds | None = None
) -> HookStats:
    """The hook figures over ``results`` (every transcript in the window,
    main sessions and agents alike: a hook runs in both)."""
    lookup = pricing.resolve_model
    stats = failure_stats(results)
    for tr in results:
        _turns(tr, lookup, stats)
    return stats


# -- report section -----------------------------------------------------------


def _cause_text(row: HookRow) -> str:
    if not row.failed:
        return ""
    text = _CAUSE_WORDS.get(row.cause, "error")
    if row.cause == "not-found" and row.unexpanded:
        text += " (%VAR% not expanded)"
    elif row.cause == "not-found" and row.relative:
        text += " (relative path)"
    return text


def build_section(stats: HookStats, thresholds: HookThresholds | None = None) -> Section:
    th = thresholds or _DEFAULT_THRESHOLDS
    ranked = stats.ranked()
    failing = [h for h in ranked if h.failed]
    summary = Table(
        name="hooks_summary",
        title="Your hooks",
        columns=[
            Column(key="scope", label="Scope", kind="str"),
            Column(key="hooks", label="Hooks seen", kind="int"),
            Column(key="failing_hooks", label="Hooks that failed", kind="int"),
            Column(key="failed", label="Failed runs", kind="int"),
            Column(key="failed_wait_secs", label="Time waited on failed runs", kind="secs"),
            Column(key="blocks", label="Calls blocked", kind="int"),
            Column(key="resent", label="Sent again unchanged", kind="int"),
            Column(key="block_usd", label="Cost of blocks", kind="money"),
            Column(key="context_tokens", label="Context added", kind="tokens"),
            Column(key="carry_usd", label="Cost of keeping that context", kind="money"),
        ],
        rows=[
            [
                "your hooks",
                len(ranked),
                len(failing),
                sum(h.failed for h in ranked),
                sum(h.failed_wait_ms for h in ranked) / 1000.0,
                sum(h.blocks for h in ranked),
                sum(h.resent for h in ranked),
                sum(h.block_usd for h in ranked),
                sum(h.context_tokens for h in ranked),
                sum(h.carry_usd for h in ranked),
            ]
        ],
    )
    by_script = Table(
        name="hooks_by_script",
        title="Each hook",
        columns=[
            Column(key="hook", label="Hook", kind="str"),
            Column(key="events", label="Runs on", kind="str"),
            Column(key="failed", label="Failed runs", kind="int"),
            Column(key="cause", label="Why it failed", kind="str"),
            Column(key="failed_sessions", label="Sessions it failed in", kind="int"),
            Column(key="last_failed", label="Last failed", kind="str"),
            Column(key="stopped", label="Stopped failing?", kind="str"),
            Column(key="worked", label="Runs seen working", kind="int"),
            Column(key="blocks", label="Calls blocked", kind="int"),
            Column(key="resent", label="Sent again unchanged", kind="int"),
            Column(key="block_usd", label="Cost of blocks", kind="money"),
            Column(key="contexts", label="Times it added context", kind="int"),
            Column(key="context_tokens", label="Context added", kind="tokens"),
            Column(key="carry_usd", label="Cost of keeping its context", kind="money"),
            Column(key="wait_secs", label="Time waited", kind="secs"),
        ],
        rows=[
            [
                h.label,
                ", ".join(sorted(h.events)),
                h.failed,
                _cause_text(h),
                len(h.failed_sessions),
                h.last_failed,
                ("yes" if h.stopped(th) else "no") if h.failed else "",
                h.worked,
                h.blocks,
                h.resent,
                h.block_usd,
                h.contexts,
                h.context_tokens,
                h.carry_usd,
                h.wait_ms / 1000.0,
            ]
            for h in ranked[: th.top_n]
        ],
    )
    notes = list(ASSUMPTIONS) + [f"Thresholds: {' '.join(th.describe())}"]
    if stats.built_in_tokens:
        notes.append(
            f"Claude Code's own hooks added {stats.built_in_tokens:,} tokens of context too "
            f"(${stats.built_in_usd:,.2f} at list price to keep), not listed: you can't change them."
        )
    if len(ranked) > th.top_n:
        notes.append(f"{len(ranked) - th.top_n} more hooks aren't listed.")
    return Section(key="hooks", title="Your hooks", tables=[summary, by_script], notes=notes)


# -- recommendation rules -----------------------------------------------------


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in report.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _rows(report: ReportModel) -> list[dict]:
    """``hooks_by_script``'s rows as ``{column key: value}``."""
    table = _table(report, "hooks", "hooks_by_script")
    if table is None:
        return []
    return [{col.key: value for col, value in zip(table.columns, row)} for row in table.rows]


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _names(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " and ".join(labels)
    return f"{labels[0]}, {labels[1]} and {len(labels) - 2} more"


def _rule_hook_failures(report: ReportModel, th: HookThresholds) -> list[Recommendation]:
    """``hook-failures``: hooks with at least ``min_failures`` failed runs
    that haven't stopped failing. One card for all of them; the action
    follows the top hook's cause."""
    failing = [
        r for r in _rows(report) if _num(r.get("failed")) >= th.min_failures and r.get("stopped") != "yes"
    ]
    if not failing:
        return []
    failing.sort(key=lambda r: -_num(r.get("failed")))
    labels = [str(r["hook"]) for r in failing]
    failed = int(sum(_num(r.get("failed")) for r in failing))
    top = failing[0]
    cause = str(top.get("cause") or "")
    if len(failing) == 1:
        title = f"Your hook {labels[0]} failed {failed:,} times"
    else:
        title = f"{len(failing)} of your hooks failed {failed:,} times: {_names(labels)}"
    sessions = int(_num(top.get("failed_sessions")))
    why = (
        f"A hook that fails doesn't do its job. {labels[0]} failed in {sessions:,} sessions"
        + (f", most recently at {top['last_failed']}" if top.get("last_failed") else "")
        + (f": {cause}." if cause else ".")
    )
    worked = int(_num(top.get("worked")))
    if worked:
        why += f" It worked {worked:,} times, so it fails only some of the time."
    if top.get("last_failed"):
        why += " If you've fixed it since, this goes once it has run enough times without failing."
    if "not expanded" in cause:
        action = (
            "Its command uses a Windows %VAR% variable, which the shell Claude Code runs hooks in leaves as it is. "
            f'Use $HOME and forward slashes instead, as in "$HOME/.claude/hooks/{labels[0]}".'
        )
    elif "relative path" in cause:
        action = (
            "Its command names the script by a relative path, which only works when Claude Code runs from the "
            'project root. Start the path with ${CLAUDE_PROJECT_DIR}, as in "${CLAUDE_PROJECT_DIR}/.claude/hooks/'
            f'{labels[0]}".'
        )
    elif cause.startswith("script not found"):
        action = "Check the path in the hook's command: the script or program it names wasn't there when it ran."
    elif cause == "timed out":
        action = "Make the hook faster, or raise its timeout in the hook's settings."
    else:
        action = "Run the hook's command yourself to see the error, then fix the script."
    if len(failing) > 1:
        action += " Check the other hooks listed in the same way."
    evidence = [
        _evidence(f"{r['hook']}: failed runs", int(_num(r.get("failed"))), "hooks", "hooks_by_script", r["hook"])
        for r in failing[:5]
    ]
    return [
        Recommendation(
            id="hook-failures",
            severity="action",
            category="workflow",
            archetypes=(),
            title=title,
            why=why,
            action=action,
            lever=None,
            evidence=evidence,
        )
    ]


def _rule_hook_block_resent(report: ReportModel, th: HookThresholds) -> list[Recommendation]:
    """``hook-block-resent``: a hook that blocked at least ``min_blocks``
    calls, at least ``resend_share_pct`` of which Claude sent again
    unchanged. One card per hook; the saving is what those blocks cost."""
    out: list[Recommendation] = []
    for r in _rows(report):
        blocks = int(_num(r.get("blocks")))
        resent = int(_num(r.get("resent")))
        if blocks < th.min_blocks or 100.0 * resent / blocks < th.resend_share_pct:
            continue
        label = str(r["hook"])
        block_usd = _num(r.get("block_usd"))
        saving = block_usd * min(1.0, resent / blocks)
        out.append(
            Recommendation(
                id="hook-block-resent",
                severity="advice",
                category="workflow",
                archetypes=(),
                title=f"Hook {label} blocks calls that Claude then sends again unchanged",
                why=(
                    f"It blocked {blocks:,} calls, and Claude sent {resent:,} ({100.0 * resent / blocks:.0f}%) of them "
                    "again just as they were. Each of those blocks cost a reply that changed nothing."
                ),
                action=(
                    "If Claude only needs to know something before the call runs, let the call through and pass the "
                    "message as additionalContext in the hook's JSON output. To change the call instead, return "
                    "updatedInput. Either way, no reply is spent sending it again."
                ),
                lever=None,
                saving_usd=saving if saving > 0 else None,
                evidence=[
                    _evidence("Calls blocked", blocks, "hooks", "hooks_by_script", label),
                    _evidence("Sent again unchanged", resent, "hooks", "hooks_by_script", label),
                    _evidence("Cost of blocks", block_usd, "hooks", "hooks_by_script", label),
                ],
            )
        )
    return out


def _rule_hook_context_carry(report: ReportModel, th: HookThresholds) -> list[Recommendation]:
    """``hook-context-carry``: a hook (not ClaudeGlass's own capture hook)
    that added context at least ``min_contexts`` times, costing at least
    ``min_context_usd`` to keep. One card per hook; the saving is at most
    that cost."""
    out: list[Recommendation] = []
    for r in _rows(report):
        label = str(r["hook"])
        contexts = int(_num(r.get("contexts")))
        carry_usd = _num(r.get("carry_usd"))
        if label == HOOK_SCRIPT or contexts < th.min_contexts or carry_usd < th.min_context_usd:
            continue
        tokens = int(_num(r.get("context_tokens")))
        out.append(
            Recommendation(
                id="hook-context-carry",
                severity="advice",
                category="workflow",
                archetypes=(),
                title=f"Context from hook {label} is costly to keep",
                why=(
                    f"It added context {contexts:,} times, about {tokens:,} tokens in all, and every later reply "
                    "read it again until the next summary."
                ),
                action=(
                    "Make the hook's message shorter, or have it add context only when something needs Claude's "
                    "attention. If the hook comes from a plugin, turn it off for projects that don't need it."
                ),
                lever=None,
                saving_usd=carry_usd,
                evidence=[
                    _evidence("Times it added context", contexts, "hooks", "hooks_by_script", label),
                    _evidence("Context added (tokens)", tokens, "hooks", "hooks_by_script", label),
                    _evidence("Cost of keeping its context", carry_usd, "hooks", "hooks_by_script", label),
                ],
            )
        )
    return out


RULES: list[Callable[[ReportModel, HookThresholds], list[Recommendation]]] = [
    _rule_hook_failures,
    _rule_hook_block_resent,
    _rule_hook_context_carry,
]


__all__ = [
    "ASSUMPTIONS",
    "BUILT_IN",
    "HookThresholds",
    "HookRow",
    "HookStats",
    "compute_hook_costs",
    "build_section",
    "RULES",
]

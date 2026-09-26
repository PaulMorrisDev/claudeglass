"""Workflow-run parsing and cost linking (WP8).

Reads ``<project_dir>/<session_id>/workflows/wf_*.json`` (Workflow/
ultracode run metadata; see ``discovery.find_workflows``, which already
lists these files) into a ``WorkflowRun``, and links a run to the
subagent transcripts its own agents produced so ``topology.py`` can cost
a workflow the same way it costs a skill roll-up.

Observed real-corpus shape (30 files, this account, 2026-09-18), key
names only — never the content behind them:

    runId: str            taskId: str           script: str (workflow
    scriptPath: str        result: dict           source — full task
    agentCount: int        logs: list              prompts/instructions;
    durationMs: int        summary: str            never read here)
    workflowName: str      status: str             ("completed"|"killed"
                                                      observed)
    startTime: int (epoch ms)   defaultModel: str
    phases: list[{title, detail}]   totalTokens: int
    workflowProgress: list[{type, index, title, ...}]   totalToolCalls: int
    error: <present only on some killed/failed runs>

``script`` in particular is the workflow's full JS source, which embeds
the task's prompts, instructions and repo context verbatim (confirmed by
inspection) — it, ``logs``, ``summary``, ``result`` and
``workflowProgress`` entry titles are exactly the "prompts or results"
the plan says never to store, so ``parse_workflow_file`` never reads
them into anything returned.

A workflow's own agents live one directory level deeper than an ordinary
subagent: ``<session_id>/subagents/workflows/<run_id>/agent-*.jsonl``
(confirmed by inspection; ``discovery.find_subagents`` does not glob this
location, only the shallower ``<session_id>/subagents/agent-*.jsonl``).
Their sibling ``.meta.json`` files were observed to carry only
``agentType``/``spawnDepth``/``model`` — no ``toolUseId``, no
``description`` — so they cannot be linked to an invoking turn the way
``topology.index_tool_use_ids`` links an ordinary subagent to its parent
turn. ``link_workflow_agents`` instead matches purely by path: a
transcript belongs to a run when its immediate parent directory is named
``run.run_id``, which holds regardless of what (possibly wrong, for this
nested layout) ``session_id``/``agent_id`` ``discovery.load_meta``
derived for it from the file's grandparent directory name.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .model import Column, Section, Table, TranscriptResult, WorkflowRun
from .pricing import Pricing, price_turn

#: Rows shown in :func:`build_section`'s per-run detail table.
_PER_RUN_TABLE_LIMIT = 20


def _iso_z(dt: datetime) -> str:
    """Render a UTC ``datetime`` as ``2026-09-18T12:00:00.000Z``, matching
    the millisecond-precision ``Z``-suffixed form seen throughout the rest
    of the corpus (``turn_line``'s default timestamp, transcript
    ``timestamp`` fields).
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _transcript_cost(result: TranscriptResult, rates_lookup: Pricing) -> float:
    """Sum ``price_turn`` over every priced turn in one transcript.

    Deliberately duplicated (rather than imported) in ``topology.py``:
    both modules need this one-line rollup and importing across the two
    sibling WP8 modules for a helper this small would add a coupling
    neither otherwise has.
    """
    total = 0.0
    for turn in result.turns:
        if turn.turn_index == 0:  # a synthetic reply or missing usage, never priced
            continue
        resolved = rates_lookup.resolve_model(turn.model)
        total += price_turn(turn, resolved).total
    return total


def parse_workflow_file(path: str | Path) -> WorkflowRun:
    """Parse one ``<session_id>/workflows/wf_*.json`` run file into a
    ``WorkflowRun``.

    Reads only: ``runId`` (-> ``run_id``), the file's own path (->
    ``session_id``, the grandparent directory name per the documented
    layout), ``agentCount`` (-> ``agent_count``), the *count* of entries
    in ``phases`` (-> ``phases``) plus each entry's ``title`` only, never
    ``detail`` (-> ``phase_titles``; ``detail`` carries workflow
    source/prompt text and is never read into anything returned), and
    ``startTime``/``durationMs`` (-> ``started``/``finished``, converted
    from epoch milliseconds to the corpus's ISO-``Z`` timestamp
    convention). ``cost`` starts at ``0.0``; :func:`link_workflow_agents`
    fills it in once the run's subagent transcripts are known.

    ``status`` (``"completed"``/``"killed"`` observed) is read from the
    file straight across to ``WorkflowRun.status`` (batch C addition).

    Never raises: an unreadable or malformed file yields a ``WorkflowRun``
    with ``run_id`` taken from the filename stem and every other field at
    its default — the same tolerant-parsing posture as
    ``discovery.load_meta``.
    """
    path = Path(path)
    run_id = path.stem  # "wf_<hex>" fallback if the file can't be read/parsed
    session_id = path.parent.parent.name if path.parent.name == "workflows" else ""

    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    raw_run_id = raw.get("runId")
    if isinstance(raw_run_id, str) and raw_run_id:
        run_id = raw_run_id

    agent_count_raw = raw.get("agentCount")
    agent_count = agent_count_raw if isinstance(agent_count_raw, int) else 0

    phases_raw = raw.get("phases")
    phase_titles: list[str] = []
    if isinstance(phases_raw, list):
        phases = len(phases_raw)
        for entry in phases_raw:
            if isinstance(entry, dict):
                title = entry.get("title")
                if isinstance(title, str) and title:
                    phase_titles.append(title)
    elif isinstance(phases_raw, int):
        phases = phases_raw
    else:
        phases = 0

    status_raw = raw.get("status")
    status = status_raw if isinstance(status_raw, str) else None

    started: str | None = None
    finished: str | None = None
    start_time = raw.get("startTime")
    duration_ms = raw.get("durationMs")
    if isinstance(start_time, (int, float)):
        start_dt = datetime.fromtimestamp(start_time / 1000, tz=timezone.utc)
        started = _iso_z(start_dt)
        if isinstance(duration_ms, (int, float)):
            finished = _iso_z(start_dt + timedelta(milliseconds=duration_ms))
    if started is None:
        ts_raw = raw.get("timestamp")
        if isinstance(ts_raw, str) and ts_raw:
            started = ts_raw

    return WorkflowRun(
        run_id=run_id,
        session_id=session_id,
        agent_count=agent_count,
        phases=phases,
        started=started,
        finished=finished,
        cost=0.0,
        status=status,
        phase_titles=tuple(phase_titles),
    )


def link_workflow_agents(
    run: WorkflowRun,
    subs: list[TranscriptResult],
    rates_lookup: Pricing,
) -> list[TranscriptResult]:
    """Attach the subagent transcripts that belong to one workflow run,
    and sum their cost into ``run.cost`` (mutated in place).

    Matches by path, not by ``meta.agent_id``/``meta.tool_use_id`` (see
    the module docstring for why the workflow-nested ``.meta.json``
    shape rules that out): a transcript in ``subs`` belongs to ``run``
    when its immediate parent directory is named ``run.run_id``.

    ``subs`` is expected to be every subagent transcript the caller has
    for the session (ordinary and workflow-nested alike) — this function
    only filters and prices; it does not discover files itself.

    Returns the matched transcripts (also useful to a caller wanting to
    fold them into a skill roll-up or other topology accounting without
    re-deriving the same filter).
    """
    matched = [
        sub for sub in subs if sub.meta.path and Path(sub.meta.path).parent.name == run.run_id
    ]
    run.cost = sum(_transcript_cost(sub, rates_lookup) for sub in matched)
    return matched


# -- report section -----------------------------------------------------


def build_section(runs: Sequence[WorkflowRun]) -> Section:
    """Build the "Workflows" report section (fix item 10): a summary
    table (run/agent counts, mean cost), a status-mix table, and a
    per-run detail table capped at ``_PER_RUN_TABLE_LIMIT`` rows, sorted
    by cost descending.

    Never reads ``phase_titles``, ``script``, or any other field the
    module docstring flags as carrying prompt/source text — only the
    counts and identifiers already on ``WorkflowRun``.
    """
    total_runs = len(runs)
    total_agents = sum(r.agent_count for r in runs)
    total_cost = sum(r.cost for r in runs)
    costs = [r.cost for r in runs]
    agent_counts = [r.agent_count for r in runs]

    summary_table = Table(
        name="workflows_summary",
        title="Workflow summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="value", label="Value", kind="str"),
        ],
        rows=[
            ["Total workflow runs", total_runs],
            ["Total agents spawned", total_agents],
            ["Total cost (USD)", total_cost],
            ["Mean agents per run", statistics.mean(agent_counts) if agent_counts else None],
            ["Mean cost per run (USD)", statistics.mean(costs) if costs else None],
        ],
    )

    status_mix: dict[str, int] = {}
    for run in runs:
        key = run.status or "unknown"
        status_mix[key] = status_mix.get(key, 0) + 1
    status_table = Table(
        name="workflows_status_mix",
        title="Status mix",
        columns=[
            Column(key="status", label="Status", kind="str"),
            Column(key="count", label="Count", kind="int"),
            Column(key="pct", label="Share", kind="pct"),
        ],
        rows=[
            [status, count, 100.0 * count / total_runs if total_runs else None]
            for status, count in sorted(status_mix.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )

    ordered_runs = sorted(runs, key=lambda r: r.cost, reverse=True)
    detail_rows = [
        [
            run.run_id,
            run.session_id,
            run.status or "unknown",
            run.agent_count,
            run.phases,
            run.cost,
            run.started or "",
            run.finished or "",
        ]
        for run in ordered_runs[:_PER_RUN_TABLE_LIMIT]
    ]
    detail_notes = []
    if total_runs > _PER_RUN_TABLE_LIMIT:
        detail_notes.append(
            f"Showing the {_PER_RUN_TABLE_LIMIT} costliest of {total_runs} workflow runs."
        )
    detail_table = Table(
        name="workflows_detail",
        title=f"Top {_PER_RUN_TABLE_LIMIT} workflow runs by cost",
        columns=[
            Column(key="run_id", label="Run", kind="str"),
            Column(key="session_id", label="Session", kind="str"),
            Column(key="status", label="Status", kind="str"),
            Column(key="agent_count", label="Agents", kind="int"),
            Column(key="phases", label="Phases", kind="int"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="started", label="Started", kind="str"),
            Column(key="finished", label="Finished", kind="str"),
        ],
        rows=detail_rows,
        notes=detail_notes,
    )

    section_notes = []
    if not runs:
        section_notes.append("No workflow runs found in this window.")
    return Section(
        key="workflows",
        title="Workflows",
        tables=[summary_table, status_table, detail_table],
        notes=section_notes,
    )


__all__ = ["parse_workflow_file", "link_workflow_agents", "build_section"]

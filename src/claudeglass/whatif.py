"""What if? The estimated effect of a set of settings and agent changes
on the report's own window, looked up in tables the report already
computes -- no new simulation:

- a model for the main session or an agent: ``model_swap_by_agent_type``
  for the main session, and ``model_swap_agent_file_runs`` for an agent,
  since its file's ``model`` decides only the runs started without a
  model of their own (the same tokens repriced);
- ``autoCompactWindow``: ``compaction_sim_by_window`` (sessions replayed
  with that window; EST-P2 -- not estimated past
  ``CompactionSimThresholds().max_compactions_per_session``, the same
  floor the compaction-window rule and the profile goals hold every
  candidate window to);
- a cache lifetime (``promptCacheTtl``, ``subagentPromptCacheTtl``, an
  agent's ``experimental.cacheTtl``): ``ttl_by_agent_type`` (every cache
  write replayed at 5 minutes or 1 hour);
- an agent's ``omitClaudeMd``: the greater of ``agent_startup_breakdown``
  (the CLAUDE.md tokens each spawn writes -- the floor) and EST-P10's
  own carry cost from ``ReportModel.context_files`` (the same cache
  reads until it's re-sent that carrying any other text costs). Managed
  policy CLAUDE.md is excluded from both (PROF-11/F13 -- it still loads
  regardless of ``omitClaudeMd``);
- ``skillOverrides`` and ``enabledPlugins``: each skill's listing cost
  from ``ReportModel.context_files``;
- effort: no estimate, only how much of that agent's output was
  thinking (``topology_effort_by_agent_type``), since how much less a
  lower effort thinks isn't measured.
- ``fastMode`` turned off: ``pricing_fast_applied`` (PROF-08 -- every
  reply this window actually billed at a fast-mode rate, repriced at
  its model's standard rate). Turning it *on* isn't estimated: there is
  no per-reply "would this one have been sped up" figure for replies
  that weren't already fast.

Each row says how it was worked out (``fidelity``): "ceiling",
"simulated", "measured per spawn", "estimated" or "not estimated". A
negative ``saving_usd`` means the change costs more.

EST-P6: given a ``calibration`` lookup (``backtest.calibration_multipliers``,
built from judged predictions -- ``estimate``'s own caller passes it in,
since this module stays "look it up in tables already computed", never
touching the store itself), a row whose ``(agent, key)`` has learned a
multiplier is scaled by it and its fidelity becomes "calibrated": this
kind of change's estimate, adjusted by how it actually turned out for
you before, not just repriced or replayed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .compaction_sim import CompactionSimThresholds
from .units import Units

TOP = "top-level"

FIDELITY_TEXT = {
    # E3/EST-P1: a model reprice is a *ceiling* on the saving, not a
    # simulation -- it assumes the same tokens at the new model's rate,
    # but a different model may need more (or fewer) replies for the
    # same work, which this doesn't capture. Kept distinct from
    # "simulated" (autoCompactWindow, cache TTL) below, which replays
    # real sessions rather than just repricing their tokens.
    "ceiling": "Ceiling: the same tokens repriced at the new model's rate. The real "
    "saving could be smaller (or the change could cost more) if that model needs "
    "more replies for the same work.",
    "simulated": "Simulated: your own sessions replayed with the new value.",
    "measured": "Measured per spawn, then multiplied by the spawns in this window.",
    "estimated": "Estimated from the size of what stops being sent.",
    "none": "Not estimated.",
    # EST-P6: applied only once at least 3 of your own past predictions
    # for this exact kind of change have been judged against what
    # actually happened (backtest.calibration_multipliers) -- before
    # that, an estimate keeps its own ceiling/simulated/measured/
    # estimated fidelity above unchanged.
    "calibrated": "Adjusted by how this kind of change has actually turned out for you before (at least 3 "
    "judged predictions), not just its own ceiling, simulation or estimate.",
}


@dataclass(slots=True)
class _Tables:
    model: object

    def rows(self, section_key: str, table_name: str) -> list[dict]:
        for section in getattr(self.model, "sections", ()) or ():
            if section.key != section_key:
                continue
            for table in section.tables:
                if table.name == table_name:
                    keys = [column.key for column in table.columns]
                    return [dict(zip(keys, row)) for row in table.rows]
        return []

    def has(self, section_key: str, table_name: str) -> bool:
        """Whether the report has this table at all, rows or not."""
        return any(
            table.name == table_name
            for section in getattr(self.model, "sections", ()) or ()
            if section.key == section_key
            for table in section.tables
        )

    def row(self, section_key: str, table_name: str, agent: str) -> dict | None:
        return next((r for r in self.rows(section_key, table_name) if r.get("agent_type") == agent), None)


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _model_column(row: dict, value: str, model_ids: dict | None = None) -> str | None:
    """The ``cost_<model id>`` column a model setting ("sonnet",
    "claude-haiku-4-5", "opus[1m]", "best") prices at: the id the rate
    card's own alias table names (``model_ids``, the report's
    ``meta.model_ids``), else an exact id, else the newest id containing
    the value, compared by version number so ``claude-opus-4-10`` beats
    ``claude-opus-4-9``."""
    raw = str(value).strip()
    wanted = raw.lower().replace("[1m]", "")
    columns = [k for k in row if k.startswith("cost_claude")]
    for name in dict.fromkeys((raw, raw.lower(), wanted)):
        canonical = (model_ids or {}).get(name)
        if canonical and f"cost_{canonical}" in columns:
            return f"cost_{canonical}"
    exact = f"cost_{wanted}"
    if exact in columns:
        return exact
    matches = [k for k in columns if wanted in k[len("cost_") :]]
    if not matches:
        return None
    return max(matches, key=lambda k: (_version(k[len("cost_") :]), k))


def _version(model_id: str) -> tuple[int, ...]:
    """A model id's version numbers, its date left off: ``claude-opus-4-10``
    -> ``(4, 10)``. The twin of costs.js's ``versionOf``."""
    return tuple(int(n) for n in re.findall(r"\d+", re.sub(r"-\d{8}$", "", model_id)))


def _row(key: str, agent: str | None, value, saving: float | None, fidelity: str, basis: str) -> dict:
    return {
        "key": key,
        "agent": agent,
        "value": value,
        "saving_usd": round(saving, 6) if saving is not None else None,
        "fidelity": fidelity,
        "basis": basis,
    }


def _model(tables: _Tables, agent: str, value, key: str, label: str | None) -> dict:
    row = tables.row("model_swap", "model_swap_by_agent_type", agent)
    if row is None:
        return _row(key, label, value, None, "none", f"No {agent} runs in this window.")
    runs = ""
    if agent != TOP and tables.has("model_swap", "model_swap_agent_file_runs"):
        # An agent file's model decides only the runs started without a
        # model of their own; a workflow script or the spawn sets the rest.
        # (A report from before that table priced the whole row.)
        row = tables.row("model_swap", "model_swap_agent_file_runs", agent)
        if row is None:
            return _row(
                key, label, value, None, "none",
                f"No {agent} run in this window followed its agent file's model: a workflow script or the "
                "spawn itself named it.",
            )
        runs = " started without a model of their own"
    # A report without meta.model_ids falls back to matching column names.
    model_ids = getattr(getattr(tables.model, "meta", None), "model_ids", None) or {}
    column = _model_column(row, value, model_ids)
    observed = _num(row.get("observed_cost"))
    new = _num(row.get(column)) if column else None
    if column is None or observed is None or new is None:
        return _row(key, label, value, None, "none", f"No price for {value} in the rate card.")
    who = "the main session" if agent == TOP else f"{agent}"
    return _row(
        key,
        label,
        value,
        observed - new,
        "ceiling",
        f"Worked out by repricing the replies of {who}'s runs in this window{runs} at {column[len('cost_'):]}. "
        if runs
        else f"Worked out by repricing {who}'s replies in this window at {column[len('cost_'):]}. "
        "A different model may need more or fewer replies for the same work, which this doesn't capture.",
    )


def _opusplan(tables: _Tables, value, key: str) -> dict:
    """``model = "opusplan"``: Opus while planning, Sonnet otherwise.
    Priced from the replies after each approved plan, as they ran and at
    Sonnet's prices (``plan_handoff_summary``)."""
    row = next(iter(tables.rows("plan_handoff", "plan_handoff_summary")), None) or {}
    build, sonnet = _num(row.get("build_usd")), _num(row.get("build_usd_sonnet"))
    if not build or sonnet is None:
        return _row(key, None, value, None, "none", "No approved plans in this window, so there is no build to reprice.")
    return _row(
        key,
        None,
        value,
        build - sonnet,
        "ceiling",
        "Worked out by repricing the main session's replies after each approved plan at Sonnet's prices. "
        "With opusplan, sessions without a plan run on Sonnet too; their saving isn't counted here.",
    )


def _ttl(tables: _Tables, agents: list[str], value, key: str, label: str | None) -> dict:
    column = {"5m": "cost_all_5m", "1h": "cost_all_1h"}.get(str(value))
    if column is None:
        return _row(key, label, value, None, "none", "Only 5m and 1h are simulated.")
    saving = 0.0
    found = False
    for agent in agents:
        row = tables.row("ttl", "ttl_by_agent_type", agent)
        observed, new = (_num(row.get("cost_observed")), _num(row.get(column))) if row else (None, None)
        if observed is not None and new is not None:
            saving += observed - new
            found = True
    if not found:
        return _row(key, label, value, None, "none", "No runs in this window to replay.")
    return _row(
        key, label, value, saving, "simulated",
        f"Every cache write in this window replayed with a {value} lifetime: a longer lifetime costs more per "
        "write but rebuilds less after pauses.",
    )


def _compact(tables: _Tables, value, current) -> dict:
    rows = {str(r.get("window")).replace(",", ""): r for r in tables.rows("compaction_sim", "compaction_sim_by_window")}
    new = rows.get(str(value))
    base = rows.get(str(current)) if current is not None else rows.get("none")
    if new is None or base is None:
        options = ", ".join(k for k in rows if k != "none")
        return _row(
            "autoCompactWindow", None, value, None, "none",
            f"Only these windows are simulated: {options}." if options else "No sessions to replay.",
        )
    # EST-P2: the same floor the compaction-window rule and the profile
    # goals' own _compaction already hold every candidate to -- a window
    # that summarises more than this often per session loses too much
    # detail to trust, however cheap it simulates.
    limit = CompactionSimThresholds().max_compactions_per_session
    compactions = _num(new.get("compactions_per_session"))
    if compactions is not None and compactions > limit:
        return _row(
            "autoCompactWindow", None, value, None, "none",
            f"Not estimated: {int(value):,} tokens would summarise about {compactions:.1f} times a session, "
            f"more than the {limit:g} this project trusts a window to lose that much detail that often.",
        )
    saving = (_num(base.get("cost")) or 0.0) - (_num(new.get("cost")) or 0.0)
    return _row(
        "autoCompactWindow", None, value, saving, "simulated",
        f"Your sessions replayed with summaries at {int(value):,} tokens: about "
        f"{_num(new.get('compactions_per_session')) or 0:.1f} summaries per session. Earlier summaries carry "
        "less context each reply but lose detail.",
    )


def _claude_md_carry_usd(context_files: dict, agent: str) -> float:
    """EST-P10: what carrying CLAUDE.md actually costs -- not just the
    one cache write each spawn, but a cache read on every later turn
    until it's re-sent (``context_files._Carry``, the same accounting
    ``/api/context-files`` already reports). Summed over every file this
    ``agent`` was reached by except Managed policy CLAUDE.md (PROF-11/
    F13: still loads regardless of ``omitClaudeMd``, so never part of
    what this saves)."""
    return sum(
        _num((f.get("cost_by_reach") or {}).get(agent)) or 0.0
        for f in (context_files or {}).get("files") or ()
        if isinstance(f, dict) and f.get("type") != "Managed"
    )


def _omit_claude_md(tables: _Tables, context_files: dict, agent: str, label: str) -> dict:
    row = tables.row("agent_startup", "agent_startup_breakdown", agent)
    if not row:
        return _row("omitClaudeMd", label, True, None, "none", f"No CLAUDE.md measured at {agent}'s start.")
    # PROF-11/F13: Managed policy CLAUDE.md still loads regardless of
    # omitClaudeMd, so it's never part of what this saves.
    managed = _num(row.get("claude_md_managed")) or 0.0
    tokens = max(0.0, (_num(row.get("claude_md")) or 0.0) - managed)
    spawns, price = _num(row.get("spawns")), _num(row.get("write_price"))
    if not tokens or not spawns or not price:
        return _row("omitClaudeMd", label, True, None, "none", f"No CLAUDE.md measured at {agent}'s start.")
    floor = tokens * spawns * price / 1_000_000
    note = (
        f"About {round(tokens):,} CLAUDE.md tokens written at each of {int(spawns)} spawns. The agent then "
        "works without your project's rules."
    )
    if managed:
        note += f" Managed policy CLAUDE.md ({round(managed):,} tokens) still loads either way."
    # EST-P10: once written, CLAUDE.md is also carried -- read back from
    # cache on every later turn of that spawn until it's re-sent -- which
    # this window's own context-files accounting already prices. Never
    # below the write-only floor above: the two are measured different
    # ways (startup-window events vs. per-file inject events), so a
    # partial or missing carry reading falls back to it rather than
    # understating the saving.
    carried = _claude_md_carry_usd(context_files, agent)
    if carried > floor:
        note += " Carrying it across the rest of each spawn's turns, until it's re-sent, costs still more."
    return _row("omitClaudeMd", label, True, max(floor, carried), "measured", note)


def _skills(context_files: dict, overrides: dict) -> dict:
    skills = {row.get("name"): row for row in (context_files or {}).get("skills") or () if isinstance(row, dict)}
    saving = 0.0
    counted = []
    for name, visibility in overrides.items():
        row = skills.get(name)
        if row is None or visibility == "on":
            continue
        cost = _num(row.get("listing_cost_usd")) or 0.0
        # "name-only" keeps the name: roughly the first few tokens of a line.
        share = 1.0 if visibility in ("off", "user-invocable-only") else 0.8
        saving += cost * share
        counted.append(name)
    if not counted:
        return _row("skillOverrides", None, overrides, None, "none", "None of these skills was listed in this window.")
    return _row(
        "skillOverrides", None, overrides, saving, "estimated",
        f"The listing lines of {len(counted)} skill{'s' if len(counted) != 1 else ''}, sent at the start of "
        "every session and subagent, stop being sent (only the name stays with name-only).",
    )


def _plugins(context_files: dict, plugins: dict) -> dict:
    skills = (context_files or {}).get("skills") or ()
    saving = 0.0
    off = [name.split("@")[0] for name, enabled in plugins.items() if enabled is False]
    for row in skills:
        name = str(row.get("name") or "")
        if ":" in name and name.split(":")[0] in off:
            saving += _num(row.get("listing_cost_usd")) or 0.0
    if not off:
        return _row("enabledPlugins", None, plugins, 0.0, "estimated", "Turns plugins on: adds their skills and tools.")
    return _row(
        "enabledPlugins", None, plugins, saving or None, "estimated" if saving else "none",
        "The listing lines of those plugins' skills stop being sent. Their tools and MCP servers go too; those "
        "aren't counted here.",
    )


def _fast_mode(tables: _Tables) -> dict:
    """PROF-08: turning ``fastMode`` off, repriced from
    ``pricing_fast_applied`` -- every reply this window actually billed
    at a fast-mode rate (a documented multiplier over standard), against
    what the same replies would have cost at their model's standard rate
    instead. Only the "off" direction is simulated (see the module
    docstring): there's no measured "would this reply have been sped up"
    figure for replies that weren't already fast, so turning it *on* is
    never dispatched here."""
    rows = tables.rows("usage", "pricing_fast_applied")
    if not rows:
        return _row("fastMode", None, False, None, "none", "No fast-priced replies in this window.")
    cost = sum(_num(r.get("cost")) or 0.0 for r in rows)
    standard = sum(_num(r.get("standard_cost")) or 0.0 for r in rows)
    return _row(
        "fastMode", None, False, cost - standard, "simulated",
        "Every reply this window actually billed at a fast-mode rate, repriced at its model's standard rate. "
        "Fast mode trades a price premium for a faster reply, so this prices the trade, not the time it costs.",
    )


def _effort(tables: _Tables, agent: str | None, value, key: str) -> dict:
    row = tables.row("agents", "topology_effort_by_agent_type", agent or TOP)
    share = _num(row.get("thinking_share")) if row else None
    basis = (
        f"Thinking was {share:.0f}% of {'the main session' if not agent else agent}'s output. A lower effort thinks "
        "less, but by how much isn't measured."
        if share is not None
        else "How much less a lower effort thinks isn't measured."
    )
    return _row(key, agent, value, None, "none", basis)


def estimate(
    settings: dict,
    agents: dict,
    model,
    units: Units,
    *,
    period: str = "",
    current: dict | None = None,
    calibration: dict[tuple[str | None, str], float] | None = None,
) -> dict:
    """One row per change in ``settings`` and ``agents`` (``{agent:
    {key: value}}``), plus a total of the rows that could be estimated.
    ``current`` is the settings in effect now, where known. ``calibration``
    (EST-P6, see the module docstring) is a ``(agent, key) -> multiplier``
    lookup; a row whose pair is in it has its ``saving_usd`` scaled by
    that multiplier and its fidelity set to "calibrated" -- the value
    and fidelity it would otherwise have had move to
    ``uncalibrated_usd``/``uncalibrated_fidelity`` (``None`` on every
    other row), so a caller logging a prediction to check later
    (``route_whatif``'s ``"log": true``) can still log the raw estimate
    rather than one already adjusted by a past prediction's own outcome
    -- calibrating a calibrated number would compound, not correct."""
    tables = _Tables(model)
    current = current or {}
    context_files = getattr(model, "context_files", None) or {}
    subagents = [r.get("agent_type") for r in tables.rows("ttl", "ttl_by_agent_type") if r.get("agent_type") != TOP]
    rows: list[dict] = []
    for key, value in (settings or {}).items():
        if key == "model" and str(value).strip().lower() == "opusplan":
            rows.append(_opusplan(tables, value, key))
        elif key == "model":
            rows.append(_model(tables, TOP, value, key, None))
        elif key == "promptCacheTtl":
            rows.append(_ttl(tables, [TOP], value, key, None))
        elif key == "subagentPromptCacheTtl":
            rows.append(_ttl(tables, subagents, value, key, None))
        elif key == "autoCompactWindow":
            rows.append(_compact(tables, value, current.get("autoCompactWindow")))
        elif key == "skillOverrides" and isinstance(value, dict):
            rows.append(_skills(context_files, value))
        elif key == "enabledPlugins" and isinstance(value, dict):
            rows.append(_plugins(context_files, value))
        elif key == "effortLevel":
            rows.append(_effort(tables, None, value, key))
        elif key == "fastMode" and value is False:
            rows.append(_fast_mode(tables))
        else:
            rows.append(_row(key, None, value, None, "none", "This change isn't simulated."))
    for agent, levers in (agents or {}).items():
        for key, value in (levers or {}).items():
            if key == "model":
                rows.append(_model(tables, agent, value, key, agent))
            elif key in ("experimental.cacheTtl", "cacheTtl"):
                rows.append(_ttl(tables, [agent], value, "experimental.cacheTtl", agent))
            elif key == "omitClaudeMd" and value is True:
                rows.append(_omit_claude_md(tables, context_files, agent, agent))
            elif key == "effort":
                rows.append(_effort(tables, agent, value, key))
            else:
                rows.append(_row(key, agent, value, None, "none", "This change isn't simulated."))
    for row in rows:
        row["uncalibrated_usd"] = None
        row["uncalibrated_fidelity"] = None
    if calibration:
        for row in rows:
            if row["saving_usd"] is None:
                continue
            multiplier = calibration.get((row["agent"], row["key"]))
            if multiplier is not None:
                row["uncalibrated_usd"] = row["saving_usd"]
                row["uncalibrated_fidelity"] = row["fidelity"]
                row["saving_usd"] = round(row["saving_usd"] * multiplier, 6)
                row["fidelity"] = "calibrated"
    total = sum(row["saving_usd"] for row in rows if row["saving_usd"] is not None)
    for row in rows:
        row["effect_text"] = _effect_text(row["saving_usd"], units, period)
        row["fidelity_text"] = FIDELITY_TEXT.get(row["fidelity"], "")
    estimated = [row for row in rows if row["saving_usd"] is not None]
    return {
        "period": period,
        "rows": rows,
        "total_usd": round(total, 6),
        "total_text": _effect_text(total, units, period) if estimated else "",
        "estimated": len(estimated),
        "not_estimated": len(rows) - len(estimated),
        "total_note": (
            "Changes overlap (a cheaper model also makes each summary and cache write cheaper), so the total "
            "is rough; each row on its own is the better guide."
            if len(estimated) > 1
            else ""
        ),
    }


def _effect_text(saving: float | None, units: Units, period: str) -> str:
    if saving is None:
        return "Not estimated"
    amount = units.money(abs(saving))
    if amount is None:
        return "No measurable change"
    suffix = f" {period}" if period else ""
    return f"Saves {amount.text()}{suffix}" if saving > 0 else f"Costs {amount.text()} more{suffix}"


__all__ = ["FIDELITY_TEXT", "estimate"]

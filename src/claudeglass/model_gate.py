"""One veto-and-gate helper for model-swap suggestions (audit finding
F9: "model-switch gates differ across goals, habits, model_swap and
quality").

Every place that offers "agent X would have cost less on model Y" --
the corpus-wide models goal, a single task's candidate, the
``model-tier`` recommendation, and the quick-actions tip that explains
why a cheaper model wasn't offered -- ran its own copy of the same
three-way check: did the quality section find this setup did clearly
worse on that model (``quality.worse_models``), were its runs on that
model often retried on a larger one (``quality.retried_models``,
:data:`quality.RETRIED_SHARE`), and did metrics capture say the
agent's work was mostly hard or a run was retried for the model
(``habits.unfit_agents``). This module is the one copy.

It also closes a fourth gap the corpus-wide ``unfit_agents`` check
never covered: a *task's own* runs being mostly hard or retried for the
model, even when the agent isn't flagged unfit overall
(:func:`row_unfit_reason`, reused per-task by
``habits._agents_by_task_table`` and ``profiles.goals._task_agents``) --
the per-task veto. Claude's own verdict of whether a larger model was
needed (``fit``) said "right" every time and is no longer a veto.

Every sample-size floor here comes from a single source,
``model_swap.ModelSwapThresholds.min_sessions`` -- the same floor
``model_swap.py``'s own rows already clear before a
``best_cheaper_alternative_model`` is even offered -- rather than each
caller inventing its own (the naive ``hard_pct``/``retried_model``
checks here previously had no floor at all: one retried run out of
one was enough to blacklist a model forever).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import quality
from .model_swap import ModelSwapThresholds


def row_unfit(row: dict, *, min_sessions: int | None = None) -> tuple[str, float] | None:
    """``(kind, figure)`` for why a habits agents row -- corpus-wide
    (``habits_agents``) or one kind of task's slice of it
    (``habits_agents_by_task``) -- shouldn't be offered a smaller model:
    ``"hard"`` (most of its work was reported hard; the figure is that
    share) or ``"retried-model"`` (a run was retried because the model
    wasn't enough; the figure is those retries). ``None`` when nothing
    vetoes it, including when its sample (``row["runs"]``) is below
    ``min_sessions``. The main session counts by how hard its work was
    only, since it has no ``retried_model`` of its own -- matching the
    long-standing ``habits.unfit_agents`` convention this replaces."""
    runs = row.get("runs")
    if min_sessions is not None and isinstance(runs, (int, float)) and runs < min_sessions:
        return None
    hard = row.get("hard_pct")
    if isinstance(hard, (int, float)) and hard >= 50:
        return "hard", hard
    if (row.get("retried_model") or 0) >= 1:
        return "retried-model", row.get("retried_model") or 0
    return None


def unfit_reason(kind: str, figure: float) -> str:
    """The clause for one :func:`row_unfit` result, for "left out
    because ..."."""
    if kind == "hard":
        return f"{figure:.0f}% of its work was reported hard"
    return "a run was retried because the model wasn't enough"


def row_unfit_reason(row: dict, *, min_sessions: int | None = None) -> str | None:
    """:func:`row_unfit` as a clause (:func:`unfit_reason`), or ``None``
    when nothing vetoes the row."""
    unfit = row_unfit(row, min_sessions=min_sessions)
    return unfit_reason(*unfit) if unfit is not None else None


def _floor(min_sessions: int | None) -> int:
    return ModelSwapThresholds().min_sessions if min_sessions is None else min_sessions


def unfit_kinds(tables, *, min_sessions: int | None = None) -> dict[str, tuple[str, float]]:
    """``{agent: (kind, figure)}`` (:func:`row_unfit`) for every
    corpus-wide habits agents row something vetoes -- the structured
    form of :func:`raw`'s ``unfit``, for a caller that groups agents by
    reason."""
    floor = _floor(min_sessions)
    out: dict[str, tuple[str, float]] = {}
    for row in tables.rows("habits", "habits_agents"):
        agent = row.get("agent_type")
        if not agent:
            continue
        unfit = row_unfit(row, min_sessions=floor)
        if unfit is not None:
            out[agent] = unfit
    return out


def raw(tables, *, min_sessions: int | None = None) -> tuple[dict, dict, dict]:
    """``(worse, retried, unfit)`` exactly as the call sites this module
    replaces used to compute them by hand (F9) -- for a caller that
    formats its own reason text per dict rather than using
    :meth:`ModelGate.reason`. ``min_sessions`` defaults to
    ``ModelSwapThresholds().min_sessions``."""
    floor = _floor(min_sessions)
    worse = quality.worse_models(tables.rows("quality", "quality_by_setup"))
    retried = quality.retried_models(tables.rows("quality", "quality_retried"), min_sessions=floor)
    unfit = {agent: unfit_reason(*kind) for agent, kind in unfit_kinds(tables, min_sessions=floor).items()}
    return worse, retried, unfit


@dataclass
class ModelGate:
    """The three corpus-wide vetoes, bundled with the one merge and
    lookup every consumer needs: is ``(agent, model family)`` vetoed at
    all, and why. ``worse``/``retried`` are keyed ``(agent, family)``
    (``"top-level"`` for the main session, matching the model-swap
    table); ``unfit`` is keyed by agent alone."""

    worse: dict[tuple, dict] = field(default_factory=dict)
    retried: dict[tuple, dict] = field(default_factory=dict)
    unfit: dict[str, str] = field(default_factory=dict)

    @property
    def combined(self) -> dict[tuple, dict]:
        """``worse``, filled in with any ``retried`` keys it doesn't
        already cover -- the merge the corpus-wide models goal has
        always used (retried is the weaker signal of the two)."""
        out = dict(self.worse)
        out.update({key: row for key, row in self.retried.items() if key not in out})
        return out

    def vetoed(self, agent: str | None, family: str) -> bool:
        """True when ``(agent, family)`` shouldn't be suggested: the
        quality check found it did worse or was often retried on a
        larger model, or the agent itself is unfit for a smaller one."""
        if (agent, family) in self.combined:
            return True
        return agent in self.unfit if agent is not None else False


def build(tables, *, min_sessions: int | None = None) -> ModelGate:
    """The gate for this report's tables (a ``whatif._Tables``), from
    the quality and habits sections it already computed."""
    worse, retried, unfit = raw(tables, min_sessions=min_sessions)
    return ModelGate(worse=worse, retried=retried, unfit=unfit)


__all__ = ["ModelGate", "row_unfit", "row_unfit_reason", "unfit_kinds", "unfit_reason", "raw", "build"]

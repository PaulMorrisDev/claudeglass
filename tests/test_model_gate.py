"""The shared model-swap veto/gate helper (``model_gate``, PROF-05):
one merge of "did worse", "often retried", and "unfit" (metrics
capture said the work was mostly hard or a run was retried for the
model), with a sample-size floor none of the four call sites this
module replaces used to have. Claude's own "needed a larger model"
verdict (``fit``) is no part of it: it said "right" every time."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace as NS

from claudeglass import advice, habits, model_gate

from test_whatif import _table


def _tables(quality_by_setup=(), quality_retried=(), habits_agents=()):
    model = NS(sections=[
        NS(key="quality", tables=[
            _table("quality_by_setup", list(quality_by_setup)) if quality_by_setup else NS(name="quality_by_setup", columns=[], rows=[]),
            _table("quality_retried", list(quality_retried)) if quality_retried else NS(name="quality_retried", columns=[], rows=[]),
        ]),
        NS(key="habits", tables=[
            _table("habits_agents", list(habits_agents)) if habits_agents else NS(name="habits_agents", columns=[], rows=[]),
        ]),
    ])
    from claudeglass import whatif
    return whatif._Tables(model)


# -- row_unfit_reason ----------------------------------------------------------


def test_row_unfit_reason_prefers_hard_over_retried():
    row = {"runs": 10, "hard_pct": 90, "retried_model": 2}
    assert model_gate.row_unfit_reason(row) == "90% of its work was reported hard"
    assert model_gate.row_unfit_reason({"runs": 10, "hard_pct": 10, "retried_model": 2}) == (
        "a run was retried because the model wasn't enough"
    )


def test_row_unfit_reason_ignores_what_an_old_row_said_about_fit():
    # Claude's verdict on its own model fit is no veto, whatever an old table still carries.
    row = {"runs": 10, "fit_larger": 8, "fit_smaller": 0, "fit_right": 2}
    assert model_gate.row_unfit_reason(row) is None
    assert model_gate.row_unfit_reason({"runs": 10, "fit_larger": 3, "hard_pct": 60}) == (
        "60% of its work was reported hard"
    )


def test_row_unfit_reason_falls_back_to_hard_then_retried():
    assert model_gate.row_unfit_reason({"runs": 10, "hard_pct": 60}) == "60% of its work was reported hard"
    assert model_gate.row_unfit_reason({"runs": 10, "retried_model": 1}) == (
        "a run was retried because the model wasn't enough"
    )


def test_row_unfit_reason_none_when_nothing_fires():
    assert model_gate.row_unfit_reason({"runs": 10, "retried_model": 0, "hard_pct": 10}) is None


def test_row_unfit_reason_floor_suppresses_a_thin_sample():
    # 1 retried run of 2 -- without a floor this blacklists the
    # model forever off a single retry-worthy anecdote.
    row = {"runs": 2, "retried_model": 1}
    assert model_gate.row_unfit_reason(row) == "a run was retried because the model wasn't enough"
    assert model_gate.row_unfit_reason(row, min_sessions=5) is None


# -- raw / build -----------------------------------------------------------


def test_raw_merges_retried_only_where_worse_is_silent():
    tables = _tables(
        quality_by_setup=[
            {"agent_type": "reviewer", "model": "claude-haiku-4-5", "compared_model": "claude-sonnet-5",
             "setup_verdict": "worse"},
        ],
        quality_retried=[
            {"agent_type": "reviewer", "model": "claude-haiku-4-5", "runs": 10, "retried": 5, "retried_on": "claude-sonnet-5"},
            {"agent_type": "implementer", "model": "claude-haiku-4-5", "runs": 10, "retried": 5, "retried_on": "claude-sonnet-5"},
        ],
        habits_agents=[{"agent_type": "planner", "runs": 10, "hard_pct": 80, "retried_model": 0}],
    )
    worse, retried, unfit = model_gate.raw(tables)
    assert ("reviewer", "haiku") in worse
    assert ("reviewer", "haiku") in retried  # both computed independently
    assert ("implementer", "haiku") in retried
    assert unfit == {"planner": "80% of its work was reported hard"}


def test_raw_applies_the_model_swap_min_sessions_floor_to_retried_and_unfit():
    tables = _tables(
        quality_retried=[{"agent_type": "reviewer", "model": "claude-haiku-4-5", "runs": 2, "retried": 1,
                           "retried_on": "claude-sonnet-5"}],
        habits_agents=[{"agent_type": "planner", "runs": 2, "hard_pct": 100, "retried_model": 1}],
    )
    worse, retried, unfit = model_gate.raw(tables)
    assert retried == {}
    assert unfit == {}


def test_build_vetoed_checks_worse_retried_and_unfit():
    tables = _tables(
        quality_by_setup=[{"agent_type": "reviewer", "model": "claude-haiku-4-5", "compared_model": "claude-sonnet-5",
                            "setup_verdict": "worse"}],
        habits_agents=[{"agent_type": "planner", "runs": 10, "hard_pct": 80, "retried_model": 0}],
    )
    gate = model_gate.build(tables)
    assert gate.vetoed("reviewer", "haiku") is True
    assert gate.vetoed("planner", "sonnet") is True  # unfit blocks every family, not just the compared one
    assert gate.vetoed("reviewer", "sonnet") is False
    assert gate.vetoed(None, "haiku") is False


# -- the retired fit verdict ----------------------------------------------------


def test_nothing_reads_claudes_model_fit_verdict_any_more():
    """Claude's ``fit`` word said "right" every time. The only code left
    that names it reads what an old row or reply carried (the tag parsers,
    the retired vocabulary): no table, the model-swap evidence, the gate or
    the help copy has a ``fit`` column or reads one."""
    src = Path(__file__).resolve().parents[1] / "src" / "claudeglass"
    pattern = re.compile(r"fit_smaller|fit_right|fit_larger|\.fit\b|[\"']fit[\"']\s*[:\]]|\bfit=")
    consumers = [
        "habits.py", "model_gate.py", "model_swap.py", "helptext.py", "advice.py", "quick_actions.py",
        "profiles/goals.py", "whatif.py", "recommend.py", "report.py", "quality.py",
    ]
    for name in consumers:
        path = src / name
        assert path.is_file(), name
        assert not pattern.search(path.read_text(encoding="utf-8")), f"{name} still reads the fit verdict"
    assert "fit" not in habits.AgentFact.__dataclass_fields__
    assert "larger" not in dict(advice._LEFT_OUT_GROUPS)
    # An old table's counts of what Claude said about the model are no veto, however many.
    assert model_gate.row_unfit_reason({"runs": 99, "fit_larger": 99, "fit_smaller": 0}) is None

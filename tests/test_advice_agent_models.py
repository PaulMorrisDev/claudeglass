"""``advice.finish`` writes the three agent-model cards.

The recommendations are built the way ``agent_models.RULES`` builds them
(ids, severity, variant, agent type and evidence labels, in the order of
its contract), so these tests hold the words, which live in
``advice._EXPLAIN``, and don't depend on the engine."""

from __future__ import annotations

import re
from datetime import date

import pytest

from claudeglass import advice
from claudeglass.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claudeglass.snapshots import Snapshot
from claudeglass.units import Units

_SOURCE = "model_swap.model_swap_agent_models"

_LABELS = (
    "Agents",
    "Roles",
    "Model",
    "Cost (USD)",
    "Cost on Sonnet (USD)",
    "Ceiling saving (USD)",
    "Ceiling saving (%)",
    "Edit turns",
    "Workflow runs",
    "First seen",
    "Last seen",
    "Later compliant writers",
    "CLAUDE_CODE_SUBAGENT_MODEL set",
)

_VERDICTS = {
    "agent-model-inherited": "inherited",
    "agent-model-asked": "asked",
    "agent-decide-apply": "decide-apply",
}

_UNITS = [
    pytest.param(Units(), id="api"),
    pytest.param(Units(billing_mode="subscription"), id="subscription"),
]

_SNAPSHOT = Snapshot(path=None, ts="2026-10-02T00:00:00Z", data={"agents": {}})


def _report(*sections: Section, generated_at: str = "2026-10-02T09:00:00.000Z") -> ReportModel:
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0), generated_at=generated_at),
        sections=list(sections),
        diagnostics=Diagnostics(lines=1000),
    )


def _rec(
    rule_id: str = "agent-model-inherited",
    *,
    agent_type: str = "workflow-subagent",
    severity: str = "advice",
    variant: str = "",
    agents: int = 5,
    roles: str = "implement 4, fix 1",
    model: str = "claude-opus-5-5",
    cost: float = 15.43,
    on_sonnet: float | None = 12.52,
    edit_turns: int = 57,
    workflow_runs: int = 1,
    first_seen: str = "2026-09-23",
    last_seen: str = "2026-10-01",
    later: int = 0,
    env_var_set: str = "no",
) -> Recommendation:
    """A recommendation as ``agent_models.RULES`` builds it. A decide-and-
    apply row has no price on Sonnet, so no saving."""
    saving = None if on_sonnet is None else round(cost - on_sonnet, 2)
    pct = None if saving is None else round(100 * saving / cost, 1)
    values = (
        agents, roles, model, cost, on_sonnet, saving, pct, edit_turns, workflow_runs,
        first_seen, last_seen, later, env_var_set,
    )  # fmt: skip
    row = f"{agent_type}:{_VERDICTS[rule_id]}"
    return Recommendation(
        id=rule_id,
        severity=severity,
        category="workflow",
        lever="model",
        scope="user",
        agent_type=agent_type,
        variant=variant,
        saving_usd=saving,
        evidence=[(label, value, _SOURCE, row) for label, value in zip(_LABELS, values)],
    )


def _decide(**changes) -> Recommendation:
    """A decide-and-apply row: info, with no price on Sonnet."""
    return _rec("agent-decide-apply", severity="info", on_sonnet=None, **changes)


def _finish(rec: Recommendation, units: Units | None = None, report: ReportModel | None = None) -> Recommendation:
    (card,) = advice.finish([rec], report or _report(), _SNAPSHOT, units or Units())
    return card


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


# -- agent-model-inherited ----------------------------------------------------------


@pytest.mark.parametrize("units", _UNITS)
def test_a_fixed_workflow_card_names_the_agents_the_saving_and_says_it_looks_fixed(units):
    rec = _rec(severity="info", variant="fixed", later=6)
    card = _finish(rec, units)
    assert card.title == "5 workflow agents wrote code on Opus 5.5 with no model set"
    assert card.why.startswith(
        "4 implementers and 1 fixer in 1 workflow run started with no model, most recently on 1 October. "
    )
    assert "So each ran on your main session's model, Opus 5.5." in card.why
    assert "They wrote code to a settled spec, which Sonnet does well." in card.why
    assert "Agents that decide, such as integrate and review, aren't counted: Opus suits them." in card.why
    assert card.why.endswith(
        "Since then, 6 agents that wrote code ran on Sonnet or a smaller model, so this looks fixed."
    )
    assert card.action == (
        "If this rule lives in one project's notes, copy it to ~/.claude/CLAUDE.md so every project follows it."
    )
    assert (card.severity, card.variant) == ("info", "fixed")
    assert card.estimated_saving.startswith("At most ") and "2.91" in card.estimated_saving
    assert "Sonnet's list price" in card.saving_basis and "ceiling" in card.saving_basis
    assert card.saving_usd == 2.91
    assert card.changes == []


@pytest.mark.parametrize("units", _UNITS)
def test_the_advice_card_leads_with_the_prompt_and_does_not_say_it_looks_fixed(units):
    card = _finish(_rec(later=0), units)
    assert card.severity == "advice" and card.variant == ""
    assert card.action == "Paste the prompt below so Claude sets the model on every agent it starts."
    assert "looks fixed" not in card.why and "Since then" not in card.why
    assert card.estimated_saving.startswith("At most ")


def test_a_named_agent_type_card_counts_agents_not_runs():
    card = _finish(_rec(agent_type="general-purpose", agents=3, roles="fix 2, other 1", workflow_runs=0))
    assert card.title == "3 general-purpose agents wrote code on Opus 5.5 with no model set"
    assert card.why.startswith(
        "3 general-purpose agents started with no model and wrote code, most recently on 1 October. "
    )
    assert "workflow run" not in card.why
    assert card.agent_type == "general-purpose"


def test_the_card_names_the_variable_when_it_is_set():
    card = _finish(_rec(env_var_set="yes"))
    assert "So each ran on the model CLAUDE_CODE_SUBAGENT_MODEL names, Opus 5.5." in card.why
    assert "your main session's model" not in card.why
    unset = _finish(_rec(env_var_set="no"))
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in unset.why


def test_one_agent_reads_in_the_singular():
    card = _finish(_rec(agents=1, roles="implement 1"))
    assert card.title == "1 workflow agent wrote code on Opus 5.5 with no model set"
    assert card.why.startswith("1 implementer in 1 workflow run started with no model, ")
    assert "So it ran on your main session's model, Opus 5.5." in card.why
    assert "It wrote code to a settled spec, which Sonnet does well." in card.why
    named = _finish(_rec(agent_type="general-purpose", agents=1, roles="other 1", workflow_runs=0, later=1))
    assert named.title == "1 general-purpose agent wrote code on Opus 5.5 with no model set"
    assert named.why.startswith("1 general-purpose agent started with no model and wrote code, ")
    fixed = _finish(_rec(variant="fixed", severity="info", later=1))
    assert "Since then, 1 agent that wrote code ran on Sonnet or a smaller model" in fixed.why


def test_runs_and_roles_are_counted_in_words():
    card = _finish(_rec(agents=7, roles="implement 4, fix 2, other 1", workflow_runs=2))
    assert card.why.startswith(
        "4 implementers, 2 fixers and 1 other agent that edited code in 2 workflow runs started with no model, "
    )
    many = _finish(_rec(agents=14, roles="implement 4, fix 3, apply 2, test 2, build 1, write 1, refactor 1"))
    assert many.why.startswith("4 implementers, 3 fixers and 7 more in 1 workflow run started with no model, ")


def test_the_date_carries_a_year_only_when_it_is_not_the_reports_own():
    this_year = _finish(_rec(last_seen="2026-10-01"), report=_report(generated_at="2026-10-02T09:00:00.000Z"))
    assert "most recently on 1 October." in this_year.why
    last_year = _finish(_rec(last_seen="2025-12-30"), report=_report(generated_at="2026-01-05T09:00:00.000Z"))
    assert "most recently on 30 December 2025." in last_year.why
    # With no generation time on the report, today's year stands in for it.
    today = date.today()
    undated = _finish(_rec(last_seen=f"{today.year}-01-02"), report=_report(generated_at=""))
    assert "most recently on 2 January." in undated.why
    # A value that is not a date leaves the clause out.
    blank = _finish(_rec(last_seen=""))
    assert blank.why.startswith("4 implementers and 1 fixer in 1 workflow run started with no model. ")


def test_the_model_follows_the_run_but_deciders_stay_on_opus():
    card = _finish(_rec(model="claude-fable-5-1"))
    assert card.title == "5 workflow agents wrote code on Fable 5.1 with no model set"
    assert "aren't counted: Opus suits them." in card.why and "Fable suits them" not in card.why


def test_a_card_with_no_evidence_is_left_as_the_rule_wrote_it():
    for rule_id in _VERDICTS:
        card = _finish(Recommendation(id=rule_id, severity="info", title="Plain title", action="Plain action."))
        assert (card.title, card.action, card.why) == ("Plain title", "Plain action.", "")


def _quality_section(*rows: list) -> Section:
    table = Table(
        name="quality_by_setup",
        columns=[Column(key=k, label=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
        rows=list(rows),
    )
    return Section(key="quality", title="Quality", tables=[table])


def test_the_quality_veto_demotes_a_named_agent_type_card_only():
    report = _report(
        _quality_section(
            ["implementer", "claude-sonnet-5", "worse", "claude-opus-5"],
            ["workflow-subagent", "claude-sonnet-5", "worse", "claude-opus-5"],
        )
    )
    vetoed = _finish(_rec(agent_type="implementer", agents=2, roles="implement 2", workflow_runs=0), report=report)
    assert vetoed.severity == "info"
    assert vetoed.why.endswith(
        "Your quality data says implementer may not be enough on Sonnet, so try it on a few tasks first."
    )
    # The lever for a workflow agent is the script's own call, so a quality
    # row for the "workflow-subagent" bucket does not demote its card.
    workflow = _finish(_rec(), report=report)
    assert workflow.severity == "advice" and "quality data" not in workflow.why
    # No quality row, no veto.
    clear = _finish(_rec(agent_type="implementer", agents=2, roles="implement 2", workflow_runs=0))
    assert clear.severity == "advice" and "quality data" not in clear.why


# -- agent-model-asked --------------------------------------------------------------


@pytest.mark.parametrize("units", _UNITS)
def test_the_asked_card_says_the_model_was_named_in_the_call(units):
    rec = _rec(
        "agent-model-asked",
        agent_type="claude-implementer",
        severity="info",
        agents=34,
        roles="implement 34",
        cost=200.0,
        on_sonnet=150.5,
        workflow_runs=0,
        last_seen="2026-09-30",
    )
    card = _finish(rec, units)
    assert card.title == "34 claude-implementer agents that wrote code were started on Opus 5.5"
    assert card.why == (
        "34 implementers were started with Opus 5.5 named in the call, most recently on 30 September. "
        "They wrote code to a settled spec, which Sonnet does well. "
        "If whatever starts them asks for Opus out of habit, Sonnet would cost less."
    )
    assert card.action == (
        "Check the prompt, skill or script that starts these agents. Ask for Sonnet where nothing needs Opus."
    )
    assert card.severity == "info" and card.changes == []
    assert card.estimated_saving.startswith("At most ") and "49.50" in card.estimated_saving
    assert card.saving_usd == 49.5


def test_the_asked_card_reads_in_the_singular():
    card = _finish(_rec("agent-model-asked", agents=1, roles="fix 1", severity="info"))
    assert card.title == "1 workflow agent that wrote code was started on Opus 5.5"
    assert card.why.startswith("1 fixer was started with Opus 5.5 named in the call, ")
    assert "It wrote code to a settled spec" in card.why
    assert "asks for Opus out of habit" in card.why and "starts it" in card.why
    assert "starts this agent." in card.action


# -- agent-decide-apply -------------------------------------------------------------


@pytest.mark.parametrize("units", _UNITS)
def test_the_decide_and_apply_card_has_no_saving(units):
    rec = _rec(
        "agent-decide-apply",
        agent_type="general-purpose",
        severity="info",
        agents=5,
        roles="audit 5",
        cost=26.43,
        on_sonnet=None,
        edit_turns=94,
        workflow_runs=0,
        last_seen="2026-09-23",
    )
    card = _finish(rec, units)
    assert card.title == "5 general-purpose agents decided and changed code on Opus 5.5"
    assert card.why == (
        "5 general-purpose agents started to audit and then edited code in 94 replies, most recently on "
        "23 September. Deciding and applying in one Opus agent spends Opus rates on the edits too. "
        "Splitting it lets Opus decide and Sonnet apply what was decided."
    )
    assert card.action == (
        "Split this work: an Opus agent that decides and writes the exact changes, then a Sonnet agent that "
        "applies them."
    )
    assert card.estimated_saving == "" and card.saving_usd is None
    assert card.severity == "info" and card.changes == []


def test_the_decide_and_apply_verbs_come_from_the_roles_it_counted():
    card = _finish(_decide(roles="review 3, audit 2, verify 1, judge 1"))
    assert "started to review, audit or verify and then edited code" in card.why
    words = _finish(_decide(roles="completeness 2, check 1, adversarial 1"))
    assert "started to check or challenge and then edited code" in words.why
    one = _finish(_decide(agents=1, roles="review 1", edit_turns=1))
    assert "started to review and then edited code in 1 reply, " in one.why
    assert one.title == "1 workflow agent decided and changed code on Opus 5.5"
    unnamed = _finish(_decide(roles=""))
    assert "started to decide and then edited code" in unnamed.why


def test_the_decide_and_apply_card_follows_the_family():
    card = _finish(_decide(model="claude-fable-5-1"))
    assert "Deciding and applying in one Fable agent spends Fable rates on the edits too." in card.why
    assert "Splitting it lets Opus decide and Sonnet apply what was decided." in card.why
    assert card.action == "Split this work: an Opus agent that decides and writes the exact changes, then a Sonnet agent that applies them."


# -- all three ----------------------------------------------------------------------

_ALL_ROLES = (
    "review 2, verify 2, refute 1, judge 1, decide 1, audit 1, research 1, design 1, challenge 1, critique 1, "
    "synthesise 1, plan 1, find 1, map 1, assess 1, check 1, completeness 1, explore 1, investigate 1, "
    "inventory 1, baseline 1, adversarial 1, analyse 1"
)


def _every_card() -> list[Recommendation]:
    cards = []
    for roles in (
        "implement 4, fix 1",
        "implement 4, fix 3, apply 2, test 2, build 1, write 1, migrate 1, refactor 1, other 3",
        "other 2",
    ):
        for agent_type, runs in (("workflow-subagent", 12), ("a-long-agent-type-name", 0)):
            for rule_id in ("agent-model-inherited", "agent-model-asked"):
                for variant, severity, later in (("", "advice", 0), ("fixed", "info", 14)):
                    if rule_id == "agent-model-asked" and variant:
                        continue
                    cards.append(
                        _rec(
                            rule_id, agent_type=agent_type, variant=variant, severity=severity, later=later,
                            roles=roles, workflow_runs=runs, agents=30, last_seen="2025-12-30", env_var_set="yes",
                        )  # fmt: skip
                    )
    for roles in ("audit 5", _ALL_ROLES, "baseline 3, inventory 2, adversarial 1, completeness 1"):
        for agent_type in ("workflow-subagent", "a-long-agent-type-name"):
            cards.append(_decide(agent_type=agent_type, roles=roles))
    return cards


@pytest.mark.parametrize("units", _UNITS)
def test_every_sentence_is_short_and_nothing_carries_a_brace(units):
    cards = advice.finish(_every_card(), _report(), _SNAPSHOT, units)
    assert len(cards) == len(_every_card())
    for card in cards:
        for field in (card.title, card.why, card.action):
            assert field and "{" not in field and "}" not in field, (card.id, field)
            for sentence in _sentences(field):
                assert len(sentence.split()) <= 25, (card.id, len(sentence.split()), sentence)
        assert not card.title.endswith("."), card.title
        for field in (card.estimated_saving, card.saving_basis):
            assert "{" not in field and "}" not in field


def test_the_cards_stay_through_finish_and_sort_advice_first():
    """None carries a setting change and none is an agent file's to
    override, so the consolidation and already-applied passes leave all
    three, including the workflow one, alone."""
    recs = [
        _decide(agent_type="general-purpose", roles="audit 5"),
        _rec(
            "agent-model-asked",
            agent_type="claude-implementer",
            severity="info",
            roles="implement 3",
            cost=90.0,
            on_sonnet=60.0,
        ),
        _rec("agent-model-inherited", severity="advice"),
    ]
    snapshot = Snapshot(
        path=None,
        ts="2026-10-02T00:00:00Z",
        data={"agents": {"claude-implementer": {"source": "user", "model": "sonnet"}}},
    )
    out = advice.finish(recs, _report(), snapshot, Units())
    assert [r.id for r in out] == ["agent-model-inherited", "agent-model-asked", "agent-decide-apply"]
    assert [r.severity for r in out] == ["advice", "info", "info"]
    assert all(r.changes == [] and r.title and r.why and r.action for r in out)

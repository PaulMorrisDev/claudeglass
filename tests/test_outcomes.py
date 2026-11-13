"""Did a fix that landed do what its card or profile said it would
(``outcomes``)? The reading of one fix: the lever, the method, the sessions
each side of its change point, what was measured and what was predicted,
and the guardrail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claudeglass import outcomes
from claudeglass.change_points import ChangePoint

T = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _point(*labels, ts=T, project="", source="config"):
    return ChangePoint(ts, source, "x", keys=list(labels), project=project)


# -- the lever a change names, and how it can be measured -------------------


def test_a_changes_lever_is_its_last_key_and_an_agent_only_for_an_agents_field():
    assert outcomes.lever_of({"target": "agent", "agent": "gp", "key": "tools", "value": ["Read"]}) == ("gp", "tools")
    assert outcomes.lever_of({"target": "settings", "agent": None, "key": "model", "value": "sonnet"}) == (None, "model")
    assert outcomes.lever_of({"target": "settings", "key": "env.MAX_THINKING_TOKENS", "value": "1"}) == (
        None, "MAX_THINKING_TOKENS",
    )
    # A settings change never names an agent, whatever the stored row holds.
    assert outcomes.lever_of({"target": "settings", "agent": "gp", "key": "model"}) == (None, "model")


@pytest.mark.parametrize(
    "agent, key, method",
    [
        (None, "model", "exact"),
        ("gp", "model", "exact"),
        (None, "fastMode", "exact"),
        (None, "promptCacheTtl", "exact"),
        (None, "subagentPromptCacheTtl", "exact"),
        (None, "autoCompactWindow", "exact"),
        ("gp", "promptCacheTtl", "none"),
        ("gp", "autoCompactWindow", "none"),
        ("gp", "tools", "start_context"),
        ("gp", "disallowedTools", "start_context"),
        ("gp", "omitClaudeMd", "start_context"),
        ("gp", "skills", "start_context"),
        ("gp", "mcpServers", "start_context"),
        (None, "enabledPlugins", "start_context"),
        (None, "claude_md_chars", "start_context"),
        (None, "tools", "none"),
        (None, "effortLevel", "none"),
        ("gp", "effort", "none"),
        (None, "MAX_THINKING_TOKENS", "none"),
        (None, "", "none"),
    ],
)
def test_the_method_a_lever_can_be_measured_by(agent, key, method):
    assert outcomes.method_for(agent, key) == method


def test_a_card_reads_as_one_lever_only_when_all_its_changes_agree():
    pick = outcomes.lever_for
    assert pick([("gp", "tools")]) == ("gp", "tools")
    # Two start-context fields of one agent: one lever, the first named.
    assert pick([("gp", "tools"), ("gp", "disallowedTools")]) == ("gp", "tools")
    # Different methods, or an agent's field with a setting, are not one lever.
    assert outcomes.method_for(*pick([("gp", "tools"), ("gp", "model")])) == "none"
    assert outcomes.method_for(*pick([("gp", "tools"), (None, "model")])) == "none"
    # A card with no changes names none.
    assert outcomes.method_for(*pick([])) == "none"


# -- the change point a landing matches -------------------------------------


def test_a_landing_matches_the_latest_point_at_or_before_it_that_names_its_lever():
    landed = T + timedelta(days=2)
    older = _point("agents.gp.tools", ts=T - timedelta(days=5))
    wanted = _point("agents.gp.tools", ts=T)
    other = _point("effective.effortLevel", ts=T + timedelta(days=1))
    after = _point("agents.gp.tools", ts=T + timedelta(days=3))
    undone = _point("agents.gp.tools", ts=T + timedelta(days=1), source="revert")
    found = outcomes.match_point([("gp", "tools")], set(), landed, [older, wanted, other, after, undone])
    assert found is wanted
    assert outcomes.match_point([("gp", "model")], set(), landed, [older, wanted]) is None
    # A point a minute after the landing is the same edit.
    assert outcomes.match_point([("gp", "tools")], set(), T - timedelta(seconds=30), [wanted]) is wanted


def test_a_landing_matches_only_a_point_that_applies_in_its_view():
    mine = _point("agents.gp.tools", ts=T - timedelta(days=1), project="proj-a")
    theirs = _point("agents.gp.tools", ts=T, project="proj-b")
    everywhere = _point("agents.gp.tools", ts=T - timedelta(days=3))
    landed = T + timedelta(hours=1)
    assert outcomes.match_point([("gp", "tools")], {"proj-a"}, landed, [mine, theirs, everywhere]) is mine
    assert outcomes.match_point([("gp", "tools")], {"proj-c"}, landed, [mine, theirs, everywhere]) is everywhere
    # The every-project view takes a point of any one project.
    assert outcomes.match_point([("gp", "tools")], set(), landed, [mine, theirs, everywhere]) is theirs


def test_rows_of_two_views_for_one_change_point_are_one_fix():
    everywhere = _point("agents.gp.tools")
    in_a = _point("agents.gp.tools", project="proj-a")
    rows = {"*": set(), "proj-a": {"proj-a"}, "proj-b": {"proj-b"}}
    # A point for every project: the two views' rows share a key, and the "*" row ranks first.
    assert outcomes.fix_key("spawn-tools-list", "gp", everywhere) == outcomes.fix_key("spawn-tools-list", "gp", everywhere)
    assert outcomes.view_rank(everywhere, rows["*"]) > outcomes.view_rank(everywhere, rows["proj-a"])
    # A point of one project: that project's row ranks first, the "*" row next, another project's last.
    assert outcomes.view_rank(in_a, rows["proj-a"]) > outcomes.view_rank(in_a, rows["*"]) > outcomes.view_rank(
        in_a, rows["proj-b"]
    )
    # The key tells rule, agent, point and project apart.
    keys = {
        outcomes.fix_key("spawn-tools-list", "gp", everywhere),
        outcomes.fix_key("spawn-tools-list", "gp", in_a),
        outcomes.fix_key("spawn-tools-list", "other", everywhere),
        outcomes.fix_key("spawn-claude-md", "gp", everywhere),
    }
    assert len(keys) == 4
    assert outcomes.fix_key("model", None, everywhere) == ("model", None, everywhere.iso(), "")

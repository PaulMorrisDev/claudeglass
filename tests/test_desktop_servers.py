"""``desktop_servers.py``: the closed list of the MCP servers the Claude
desktop app brings itself. The list is names only; whether a name counts
as built in for a session is decided by the callers (see
``test_tool_search.py``, ``test_cost_centres.py`` and
``test_context_budget.py``)."""

from __future__ import annotations

import pytest

from claudeglass import desktop_servers


@pytest.mark.parametrize("server", sorted(desktop_servers.BUILT_IN))
def test_every_name_on_the_list_is_built_in(server):
    assert desktop_servers.is_built_in(server)


@pytest.mark.parametrize("server", ["ccd_session", "ccd_directory", "ccd_anything_the_app_adds"])
def test_the_apps_own_session_window_and_settings_servers_share_a_prefix(server):
    assert desktop_servers.is_built_in(server)


@pytest.mark.parametrize(
    "server", ["", "terminals", "my-terminal", "Terminal", "ccd", "occd_session", "github", "claude_ai_Notes"]
)
def test_a_near_miss_is_not_on_the_list(server):
    """The list is written out, not matched by pattern, so a server of yours
    that happens to share a word with a built-in is never taken for one."""
    assert not desktop_servers.is_built_in(server)


def test_a_switch_is_known_only_for_the_servers_that_have_one():
    assert "computer use" in desktop_servers.switch("computer-use")
    assert "not verified" in desktop_servers.switch("computer-use")
    assert desktop_servers.switch("terminal") == ""
    assert desktop_servers.switch("github") == ""
    assert set(desktop_servers.SWITCHES) <= desktop_servers.BUILT_IN

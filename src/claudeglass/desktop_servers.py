"""The MCP servers the Claude desktop app brings itself.

A desktop session is offered servers nobody added: computer use, the
visualiser, the built-in browser and preview, the app's own session,
window and settings servers, the scheduler, the terminal and the
connector registry. They show up in the transcripts under their own
names, and what they cost every reply is real, but there is no
connector to disconnect and no config entry to delete, so
``tool_search`` shows them with that cost and never marks one for
removal.

The list is closed and written out here, not matched by pattern, so a
server of yours that happens to share a word with one of these is never
taken for a built-in. ``tool_search`` only counts a name from this list
as built in when a desktop-app transcript offered it and no config
snapshot lists it as yours.

Nothing here imports the rest of ClaudeGlass.
"""

from __future__ import annotations

#: The built-in servers, by name as their tools carry it
#: (``parse.mcp_name``).
BUILT_IN: frozenset[str] = frozenset({
    "computer-use",
    "visualize",
    "Claude_Browser",
    "Claude_Preview",
    "terminal",
    "scheduled-tasks",
    "mcp-registry",
})

#: The prefix of the app's own session, window, view and settings
#: servers, each named after the part of the app it drives.
BUILT_IN_PREFIX = "ccd_"

#: Where a built-in's switch is, for the few whose switch is known. Not
#: verified that turning one off stops the server being offered.
SWITCHES: dict[str, str] = {
    "computer-use": (
        "the desktop app's settings have a computer use switch, which may stop this server being offered "
        "(not verified)"
    ),
}


def is_built_in(server: str) -> bool:
    """Whether ``server`` is one the desktop app brings itself."""
    return server in BUILT_IN or server.startswith(BUILT_IN_PREFIX)


def switch(server: str) -> str:
    """Where to turn ``server`` off, or "" when no switch is known."""
    return SWITCHES.get(server, "")

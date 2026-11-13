"""Roles in place of agent names (PF1 part 1).

A profile may name a role (``[roles.writer]``) instead of one of the owner's
own agents. This module says which of the user's own agents a role goes to
and folds the role's keys into those agents, so everything downstream
(the diff, the copied prompt, ``apply``) sees an ordinary profile that names
real agent files.

The only role is the ``writer``: the agent that writes code. An agent is a
writer when its *name* says so (``agent_roles``' closed vocabulary: implement,
fix, build, test, write and their forms), none of its words is a role word
of another class (a ``fix-verifier`` is a verifier), and its ``tools`` line
lets it edit files. Nothing else about the agent is read: not its description, not its
body. A name that carries no such word is never a writer.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace

from .. import agent_roles
from ..recommend import _FILE_TOOLS, _NOT_OVERRIDABLE, read_agent_tools
from .schema import ROLES, Profile

__all__ = ["ROLE_WORDS", "Resolved", "is_writer", "join_names", "resolve", "several_writers_note", "writers"]

#: Each role in the words a user reads it as.
ROLE_WORDS = {"writer": "agent that writes code"}


def is_writer(name: str, fields: Mapping | None) -> bool:
    """Whether the agent ``name`` is the agent that writes code: its name
    holds a writer word (``agent_roles``) and no role word of another class
    (``fix-verifier`` is a verifier, not a writer), and its ``tools`` and
    ``disallowedTools`` lines (``fields``) let it use a file-editing tool.
    A tools line that is not known (no line, cut off, no snapshot) means
    every tool, so it counts as able to edit. A name in
    ``recommend._NOT_OVERRIDABLE`` is never a writer."""
    if not isinstance(name, str) or name in _NOT_OVERRIDABLE:
        return False
    if agent_roles.role_class(agent_roles.role_word(None, name, None)) != "writer":
        return False
    for token in agent_roles._TOKEN_RE.findall(name.lower()):
        word = agent_roles.FORMS.get(token)
        if word is not None and agent_roles.role_class(word) != "writer":
            return False
    fields = fields if isinstance(fields, Mapping) else {}
    tools = read_agent_tools(fields.get("tools"), fields.get("disallowedTools"))
    return any(tools.offered(tool) for tool in _FILE_TOOLS)


def join_names(names: tuple[str, ...] | list[str]) -> str:
    """``a``, ``a and b`` or ``a, b and c``."""
    names = list(names)
    return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]


def several_writers_note(names: tuple[str, ...] | list[str]) -> str:
    """The sentence for a profile whose writer role went to more than one
    agent (``names``, two or more), so nobody is surprised that each
    changed."""
    both = "both" if len(names) == 2 else "all"
    return f"{join_names(names)} are {both} named like an agent that writes code, so each gets these changes."


def writers(agents: Mapping[str, Mapping]) -> tuple[str, ...]:
    """The names in ``agents`` (name -> ``{"tools": ..., "disallowedTools":
    ...}``; other fields are ignored) that are writers, sorted."""
    return tuple(sorted(name for name, fields in agents.items() if is_writer(name, fields)))


@dataclass(frozen=True, slots=True)
class Resolved:
    #: The profile with its roles folded into ``agents``; its ``roles`` is empty.
    profile: Profile
    #: Role -> the agent names it went to.
    matched: dict[str, tuple[str, ...]]


def resolve(profile: Profile, agents: Mapping[str, Mapping]) -> Resolved:
    """Each role's keys copied to every agent :func:`writers` finds in
    ``agents``. An agent the profile also names in ``agents`` keeps its own
    value for any key it sets (explicit wins, key by key). A role that
    matches no agent adds nothing, and an empty role table adds nothing. A
    profile with no roles comes back unchanged."""
    if not profile.roles:
        return Resolved(profile, {})
    folded = {name: dict(fields) for name, fields in profile.agents.items()}
    matched: dict[str, tuple[str, ...]] = {}
    for role in ROLES:
        keys = profile.roles.get(role)
        if not keys:
            continue
        names = writers(agents)
        matched[role] = names
        for name in names:
            folded[name] = {**deepcopy(keys), **folded.get(name, {})}
    return Resolved(replace(profile, agents=folded, roles={}), matched)

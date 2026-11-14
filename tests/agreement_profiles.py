"""Seven synthetic profiles for the agreement checks (``agreement.py``): a
projects folder and a config folder each, built on ``helpers.py`` and
nothing else.

The checks compare two things the product builds from one project and
window, so a profile that builds no cards proves nothing: each one holds
what the checks read. A custom agent with a ``tools:`` line whose spawns
call some tools often and some rarely, two more with the line cut or
changed, a workflow whose agents write files, built-in agents with no
file, an info card with a saving, a hook that blocks and is sent again,
a card with a standing rule, and (in the API profile) a stored ignore. A
profile that stops building one of them fails ``test_agreement_profiles``,
rather than letting a check pass over nothing.

The eight, each a :class:`Spec`:

- ``api``: API billing, the desktop app, a status line absent;
- ``cli_status``: the CLI with a status line (``usage-log.csv``) on a subscription;
- ``bare``: no custom agents and no workflows;
- ``old_version``: Claude Code before 2.1.308, which writes no
  ``api-request-shape`` lines;
- ``capture_on``: metrics capture on, replies tagged ``[cg: ...]``;
- ``capture_off``: the same work with capture off;
- ``two_projects``: two projects, read in the all-projects view. In the first,
  the project's own settings move the main model from Opus to Sonnet before
  its fifth session, and its sessions run Sonnet from then; the second stays
  on Opus and has the newest snapshot.
- ``denied``: the same work, and the reviewer's file withholds ``Edit`` and
  ``Write`` with ``disallowedTools`` (its spawns called ``Edit`` before the
  file said so, as a line changed after the spawns would leave it).

No randomness, and every date is fixed.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import (
    attachment_line,
    ignorable_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)
SLUG = "C--Dev-demo-app"
SLUG_B = "C--Dev-demo-api"
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"

#: Where the project's files are, as the transcripts record it.
CWD = "C:\\Dev\\demo-app"


@dataclass(frozen=True)
class Spec:
    name: str
    version: str = "2.1.320"
    entrypoint: str = "claude-desktop"
    billing: str = "api"
    #: Claude Code 2.1.308 on writes the tools as they went over the wire.
    request_lines: bool = True
    custom_agents: bool = True
    workflows: bool = True
    capture: bool = False
    statusline: bool = False
    sessions: int = 8
    #: Context a main session starts with (cache written on its first reply).
    base_context: int = 150_000
    #: A second snapshot, taken later, in which the reviewer's tools line has
    #: lost ``Edit`` and the main model has changed (a custom agent whose
    #: line changed inside the window; a setting on the Changes page).
    changed_later: bool = False
    #: Ignore these recommendation ids (one card each) once the first report is built.
    ignore_ids: tuple[str, ...] = ()
    #: A second project, ``SLUG_B``, with the same sessions on the same days,
    #: and the first project's own settings moving its main model to Sonnet
    #: part-way (see :func:`_main_model`). Read in the all-projects view.
    second_project: bool = False
    #: With ``second_project``: the first project moves to Sonnet before its
    #: third session and back to Opus, in your user settings, before its sixth.
    model_back: bool = False
    #: With ``second_project``: the first project's snapshot is the newest.
    a_newest: bool = False
    #: The reviewer's file has a ``disallowedTools:`` line (``REVIEWER_DENIED``).
    denies: bool = False


SPECS: dict[str, Spec] = {
    "api": Spec("api", changed_later=True, ignore_ids=("hook-block-resent",)),
    "cli_status": Spec("cli_status", entrypoint="cli", billing="subscription", statusline=True),
    "bare": Spec("bare", custom_agents=False, workflows=False),
    "old_version": Spec("old_version", version="2.1.300", request_lines=False),
    "capture_on": Spec("capture_on", capture=True),
    "capture_off": Spec("capture_off"),
    "two_projects": Spec("two_projects", custom_agents=False, workflows=False, second_project=True),
    "denied": Spec("denied", denies=True),
}


@dataclass
class Profile:
    """A built profile: where it is on disk, and the arguments that read it."""

    spec: Spec
    root: Path
    slug: str = SLUG
    #: Agent files in the snapshot (name -> frontmatter), for the tests' own reference.
    agent_files: dict = field(default_factory=dict)

    @property
    def projects_root(self) -> Path:
        return self.root / "projects"

    @property
    def config_dir(self) -> Path:
        return self.root / "claudeglass"


# -- tools -------------------------------------------------------------------


def tool_def(name: str, size: int = 300) -> dict:
    return {"name": name, "description": "d" * size, "input_schema": {"type": "object", "properties": {}}}


_SIZES = {
    "Read": 600, "Grep": 500, "Bash": 1800, "Edit": 900, "Write": 700, "Agent": 2200, "WebFetch": 14000,
    "NotebookEdit": 14000, "Skill": 3000, "mcp__figma__get_node": 5200, "mcp__figma__get_image": 4800,
}


def tools_of(*names: str) -> list[dict]:
    return [tool_def(name, _SIZES[name]) for name in names]


MAIN_TOOLS = tools_of("Read", "Grep", "Bash", "Edit", "Write", "Agent", "WebFetch", "mcp__figma__get_node",
                      "mcp__figma__get_image")
EVERY_SUBAGENT_TOOL = ("Read", "Grep", "Bash", "Edit", "Write", "WebFetch", "NotebookEdit", "Skill",
                       "mcp__figma__get_node", "mcp__figma__get_image")


def wire(tools: list[dict]) -> list[dict]:
    """The tools as they went over the wire: descriptions cut short."""
    return [{**t, "description": t["description"][: int(len(t["description"]) * 0.62)]} for t in tools]


# -- the agents --------------------------------------------------------------

#: The reviewer's and the implementer's ``tools:`` lines, as the agent files
#: write them: a comma string and a list.
REVIEWER_LINE = "Read, Grep, Bash, Edit, Write, WebFetch, NotebookEdit, Skill"
IMPLEMENTER_LINE = ["Read", "Edit", "Bash", "Grep", "WebFetch", "NotebookEdit", "Skill", "mcp__figma__get_node"]


#: The reviewer's ``disallowedTools:`` line in the ``denied`` profile, as the hook stores a
#: line: a list. ``Edit`` is on its ``tools:`` line and was called in two of five spawns;
#: ``Write`` is on it and never called.
REVIEWER_DENIED = ["Edit", "Write"]


def _reviewer_plan(k: int) -> list[str]:
    """Read and Grep in every spawn, Edit in two of five, Bash in one of the
    run: Bash falls under the bar, Edit clears it."""
    return ["Read", "Grep"] + (["Edit"] if k % 5 < 2 else []) + (["Bash"] if k == 3 else [])


def _implementer_plan(k: int) -> list[str]:
    """Read, Edit and Bash every time, one of the Figma tools in one spawn of
    four (kept, and written on the line as the file has it), Grep once."""
    return ["Read", "Edit", "Bash"] + (["mcp__figma__get_node"] if k % 4 == 0 else []) + (["Grep"] if k == 5 else [])


def _explore_plan(k: int) -> list[str]:
    return ["Read", "Grep"]


def _general_plan(k: int) -> list[str]:
    return ["Read", "Grep", "Bash", "Edit"]


#: Agent type -> its facts. ``offered`` is what its spawns were given.
AGENTS: dict[str, dict] = {
    "reviewer": dict(custom=True, alias="sonnet", model=SONNET, effort="medium", ttl="5m", md=16000,
                     plan=_reviewer_plan, offered=("Read", "Grep", "Bash", "Edit", "Write", "WebFetch", "NotebookEdit",
                                                   "Skill")),
    "claude-implementer": dict(custom=True, alias="sonnet", model=SONNET, effort="medium", ttl="1h", md=12000,
                               plan=_implementer_plan, offered=tuple(IMPLEMENTER_LINE)),
    "Explore": dict(custom=False, alias="haiku", model=HAIKU, effort=None, ttl="5m", md=0, plan=_explore_plan,
                    offered=("Read", "Grep", "Bash", "Edit")),
    "general-purpose": dict(custom=False, alias="opus", model=OPUS, effort="high", ttl="1h", md=9000,
                            plan=_general_plan, offered=EVERY_SUBAGENT_TOOL),
}


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def tick(self, seconds: float = 4.0) -> str:
        self.t += timedelta(seconds=seconds)
        return iso(self.t)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def stamp(lines: list[dict], spec: Spec, sid: str, agent_id: str | None = None) -> list[dict]:
    for d in lines:
        d["sessionId"] = sid
        d["version"] = spec.version
        d["entrypoint"] = spec.entrypoint
        d["cwd"] = CWD
        if agent_id:
            d["agentId"] = agent_id
            d["isSidechain"] = True
    return lines


def reply(clock: Clock, content, *, model, effort, read, write5=0, write1=0, out=400, thinking=0, fresh=40,
          gap=4.0) -> dict:
    line = turn_line(
        content=content, model=model, input_tokens=fresh, output_tokens=out, cache_read_input_tokens=read,
        cache_creation_input_tokens=write5 + write1, ephemeral_5m_input_tokens=write5,
        ephemeral_1h_input_tokens=write1, timestamp=clock.tick(gap), effort=effort,
    )
    if thinking:
        line["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": thinking}
        content.insert(0, {"type": "thinking", "thinking": "", "signature": "x"})
    return line


def _tool_input(name: str, i: int) -> dict:
    if name in ("Read", "Edit", "Write"):
        inp = {"file_path": f"src/mod{i}.py"}
        if name == "Edit":
            inp.update(old_string="a", new_string="b")
        if name == "Write":
            inp["content"] = "x"
        return inp
    if name == "Grep":
        return {"pattern": "def run"}
    return {"command": f"pytest tests/test_{i}.py -q"}


# -- subagents ---------------------------------------------------------------


def subagent_lines(spec: Spec, clock: Clock, agent_type: str, aid: str, k: int, capture_tags: bool) -> list[dict]:
    a = AGENTS[agent_type]
    model, effort = a["model"], a["effort"]
    offered = tools_of(*a["offered"])
    prefix = 30_000 if a["custom"] else 24_000
    written = 19_000 + (a["md"] // 4)
    lines = [user_str_line("investigate and report " * 60, timestamp=clock.tick(0.5))]
    lines.append(attachment_line("skill_listing", rendered="s" * 3200))
    if a["md"]:
        lines.append(attachment_line("instructions", rendered="CLAUDE rules " * (a["md"] // 13)))
    lines.append(attachment_line("agent_listing_delta", rendered="a" * 600))
    lines.append(attachment_line("prompt_snapshot", systemPrompt=["s" * 9000], tools=offered))
    if spec.request_lines:
        lines.append(ignorable_line("api-request-shape", shape={"tools": wire(offered)}))
    for line in lines[1:]:
        line["timestamp"] = clock.tick(0.2)
    lines.append(reply(
        clock, [text("Starting.")], model=model, effort=effort, read=prefix,
        write5=written if a["ttl"] == "5m" else 0, write1=written if a["ttl"] == "1h" else 0, out=120, fresh=120,
    ))
    read = prefix + written
    calls = list(a["plan"](k))
    # Pad to a spawn's usual length with the first tool, which every plan calls.
    calls += [calls[0]] * (8 - len(calls))
    for i, name in enumerate(calls):
        tid = f"{aid}_t{i}"
        lines.append(reply(clock, [tool_use_block(name, tid, _tool_input(name, i))], model=model, effort=effort,
                           read=read, write5=900 if a["ttl"] == "5m" else 0, write1=900 if a["ttl"] == "1h" else 0,
                           out=260, thinking=120 if effort else 0))
        lines.append(user_block_line([tool_result_block(tid, "output " * 120)], timestamp=clock.tick(1.0)))
        read += 2200
    closing = "Done. src/mod1.py holds the change." + ("\n[result: done]" if capture_tags else "")
    lines.append(reply(clock, [text(closing)], model=model, effort=effort, read=read, out=300))
    return lines


def hook_lines(index: int, m: int, s: int, tid: str) -> list[dict]:
    out = [attachment_line("hook_success", hookName="PostToolUse:Edit", hookEvent="PostToolUse",
                           command='python "C:\\hooks\\format-check.py"', content="formatted", durationMs=1400,
                           toolUseID=tid, stdout='{"hookSpecificOutput": {"additionalContext": "' + "lint note " * 80 + '"}}')]
    out.append(attachment_line("hook_additional_context", content=["lint note " * 80], rendered="lint note " * 80,
                               hookName="PostToolUse:Edit", hookEvent="PostToolUse", toolUseID=tid))
    if (m + s + index) % 3 == 0:
        out.append(attachment_line("hook_non_blocking_error", hookName="PostToolUse:Edit", hookEvent="PostToolUse",
                                   command="python .claude/hooks/lint-guard.py", stderr="No such file or directory",
                                   exitCode=127, toolUseID=tid))
    return out


# -- main sessions -----------------------------------------------------------


def _capture_note() -> str:
    from claudeglass import capture_catalogue as catalogue

    return catalogue.note_text(["task", "brief", "level", "shift"], "main")


def _main_model(spec: Spec, slug: str, index: int) -> str:
    """The model a main session ran on: Opus, every fourth on Sonnet. In a
    two-project profile the first project's sessions run Sonnet while its
    own settings set it: from its fifth session, or (``model_back``) its
    third to fifth."""
    if spec.second_project and slug == SLUG:
        if index in (range(2, 5) if spec.model_back else range(4, spec.sessions)):
            return SONNET
    return OPUS if index % 4 != 3 else SONNET


def main_session(spec: Spec, root: Path, index: int, counts: dict[str, int], slug: str = SLUG,
                 sid_prefix: str = "sess") -> dict:
    sid = f"{sid_prefix}-{index:03d}"
    clock = Clock(START + timedelta(days=index, hours=(index * 3) % 7))
    pdir = root / "projects" / slug
    model = _main_model(spec, slug, index)
    effort = "high" if index % 5 != 4 else "medium"
    lines: list[dict] = []
    if spec.capture:
        note = _capture_note()
        wrapped = f"<system-reminder>\nSessionStart hook additional context: {note}\n</system-reminder>"
        lines.append(attachment_line("hook_additional_context", rendered=wrapped, content=[note],
                                     hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart"))
    lines += [
        attachment_line("deferred_tools_delta"),
        attachment_line("skill_listing", rendered="s" * 5200),
        attachment_line("instructions", rendered="Project rules and conventions. " * 3900),
        attachment_line("prompt_snapshot", systemPrompt=["s" * 14000], tools=MAIN_TOOLS),
    ]
    if spec.request_lines:
        lines.append(ignorable_line("api-request-shape", shape={"tools": wire(MAIN_TOOLS)}))
    for line in lines:
        line["timestamp"] = clock.tick(0.2)
    agent_cycle = list(AGENTS) if spec.custom_agents else ["Explore", "general-purpose"]
    subs: list[tuple[str, str, list[dict], str]] = []
    read = 0
    first_call = True
    for m in range(6):
        easy = ((m * 7 + index * 3) % 10) < 6
        lines.append(user_str_line(f"please handle task {m} in module {index}", origin={"kind": "human"},
                                   timestamp=clock.tick(30)))
        for s in range(5 if easy else 7):
            tid = f"tu_{sid}_{m}_{s}"
            kind = (m + s) % 6
            if kind in (0, 5) and s > 0:
                agent_type = agent_cycle[(m + s + index) % len(agent_cycle)]
                aid = f"agent-{sid}-{m}-{s}"
                counts[agent_type] = counts.get(agent_type, 0) + 1
                lines.append(reply(clock, [tool_use_block("Agent", tid, {
                    "prompt": "dig into the module", "subagent_type": agent_type, "description": "look into the module",
                })], model=model, effort=effort, read=read, write5=1500, out=500, thinking=300))
                subs.append((agent_type, aid, subagent_lines(spec, Clock(clock.t), agent_type, aid,
                                                              counts[agent_type], spec.capture), tid))
                report = "findings " * (4500 if agent_type in ("reviewer", "general-purpose") else 60)
                lines.append(user_block_line([tool_result_block(tid, "Agent report: " + report)],
                                             timestamp=clock.tick(30),
                                             toolUseResult={"status": "completed", "agentId": aid}))
            else:
                if kind in (1, 2):
                    block = tool_use_block("Read", tid, {"file_path": f"src/m{index}/f{s}.py"})
                elif kind == 3:
                    block = tool_use_block("Grep", tid, {"pattern": "class Handler"})
                elif kind == 4:
                    block = tool_use_block("Bash", tid, {"command": f"pytest tests/test_{s}.py -q"})
                else:
                    block = tool_use_block("Edit", tid, {"file_path": f"src/m{index}/f{s}.py", "old_string": "a",
                                                          "new_string": "b"})
                thinking = 1500 if effort == "high" else 400
                if first_call:
                    lines.append(reply(clock, [block], model=model, effort=effort, read=0, write5=spec.base_context,
                                       out=2000 if effort == "high" else 700, thinking=thinking))
                    read = spec.base_context
                    first_call = False
                else:
                    if block["name"] == "Bash" and (m + index) % 2 == 0:
                        # A hook blocks the call, and Claude sends the very same call again.
                        lines.append(reply(clock, [block], model=model, effort=effort, read=read, write5=300, out=300,
                                           thinking=100))
                        lines.append(user_block_line([tool_result_block(
                            tid, 'PreToolUse:Bash hook error: [python "C:\\hooks\\bash-guard.py"]: use rg instead',
                            is_error=True)], timestamp=clock.tick(1)))
                        tid = tid + "r"
                        block = tool_use_block("Bash", tid, block["input"])
                        read += 800
                    lines.append(reply(clock, [block], model=model, effort=effort, read=read, write5=1500,
                                       out=2000 if effort == "high" else 700, thinking=thinking))
                lines.append(user_block_line([tool_result_block(tid, "result " * 200)], timestamp=clock.tick(2)))
                if block["name"] == "Edit":
                    for hook in hook_lines(index, m, s, tid):
                        hook["timestamp"] = clock.tick(0.1)
                        lines.append(hook)
            read += 2600
            if s == 4 and m in (2, 5):
                lines.append(system_line("compact_boundary", timestamp=clock.tick(5), compactMetadata={
                    "preTokens": read, "postTokens": 14000, "cumulativeDroppedTokens": read - 14000,
                    "durationMs": 60000}))
                read = 20_000
        tag = ""
        if spec.capture:
            level = "easy" if easy else "hard"
            task = "docs" if easy and m % 2 == 0 else "feature" if not easy else "bugfix"
            tag = f"\n[cg: task={task} brief=clear level={level} shift={'new' if m == 0 else 'build'}]"
        lines.append(reply(clock, [text("Done with that." + tag)], model=model, effort=effort, read=read, write5=800,
                           out=2200 if effort == "high" else 600, thinking=1700 if effort == "high" else 200))
        read += 900
    stamp(lines, spec, sid)
    pdir.mkdir(parents=True, exist_ok=True)
    write_jsonl(pdir / f"{sid}.jsonl", lines)
    sub_dir = pdir / sid / "subagents"
    for agent_type, aid, sub, tid in subs:
        sub_dir.mkdir(parents=True, exist_ok=True)
        stamp(sub, spec, sid, aid)
        write_jsonl(sub_dir / f"{aid}.jsonl", sub)
        meta = {"agentType": agent_type, "model": AGENTS[agent_type]["alias"], "spawnDepth": 1, "toolUseId": tid,
                "description": "look into the module"}
        (sub_dir / f"{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"sid": sid, "end": clock.t}


#: A workflow agent's tool calls, by its number across the profile: every
#: agent reads, searches and runs a command; one edits, one writes (the third
#: agent of a run, which runs on another model than the two measured, so the
#: tools fall under the bar and are kept only because a workflow agent called
#: them).
def _workflow_plan(n: int) -> list[str]:
    return ["Read", "Grep", "Bash"] + (["Edit"] if n == 2 else []) + (["Write"] if n == 5 else [])


def write_workflow(spec: Spec, root: Path, sid: str, index: int, clock: Clock, first_agent: int) -> int:
    run_id = f"wf_{index:02d}aa-000"
    wdir = root / "projects" / SLUG / sid / "workflows"
    wdir.mkdir(parents=True, exist_ok=True)
    start = clock.t
    (wdir / f"{run_id}.json").write_text(json.dumps({
        "runId": run_id, "taskId": f"task{index}", "agentCount": 3, "durationMs": 600_000,
        "startTime": int(start.timestamp() * 1000), "workflowName": "review-and-fix", "status": "completed",
        "phases": [{"title": "find"}, {"title": "fix"}], "defaultModel": SONNET,
    }), encoding="utf-8")
    agent_dir = root / "projects" / SLUG / sid / "subagents" / "workflows" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    offered = tools_of(*EVERY_SUBAGENT_TOOL)
    for n, (alias, model, ttl) in enumerate([("sonnet", SONNET, "1h"), ("sonnet", SONNET, "1h"), ("opus", OPUS, "5m")]):
        c = Clock(start + timedelta(minutes=n * 3))
        lines = [user_str_line("review the diff " * 30, timestamp=c.tick(0.5)),
                 attachment_line("prompt_snapshot", systemPrompt=["s" * 9000], tools=offered)]
        if spec.request_lines:
            lines.append(ignorable_line("api-request-shape", shape={"tools": wire(offered)}))
        for line in lines[1:]:
            line["timestamp"] = c.tick(0.2)
        lines.append(reply(c, [text("Starting.")], model=model, effort="medium", read=22_000,
                           write5=20_000 if ttl == "5m" else 0, write1=20_000 if ttl == "1h" else 0, out=100))
        read = 42_000
        calls = _workflow_plan(first_agent + n)
        calls += [calls[0]] * (8 - len(calls))
        for i, name in enumerate(calls):
            tid = f"wf{index}{n}_{i}"
            lines.append(reply(c, [tool_use_block(name, tid, _tool_input(name, i))], model=model, effort="medium",
                               read=read, out=240))
            lines.append(user_block_line([tool_result_block(tid, "x " * 200)], timestamp=c.tick(1)))
            read += 1500
        lines.append(reply(c, [text("Finished review.")], model=model, effort="medium", read=read, out=200))
        aid = f"agent-wf{index}{n}"
        stamp(lines, spec, sid, aid)
        write_jsonl(agent_dir / f"{aid}.jsonl", lines)
        # Claude Code names an agent a workflow script starts without a type "workflow-subagent".
        (agent_dir / f"{aid}.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "spawnDepth": 1,
                                                                 "model": alias}), encoding="utf-8")
    return first_agent + 3


# -- the config folder -------------------------------------------------------


def _agent_files(spec: Spec, *, reviewer_line: str = REVIEWER_LINE) -> dict:
    if not spec.custom_agents:
        return {}
    reviewer = {"model": "sonnet", "effort": "medium", "maxTurns": 40, "description": "str(120)",
                "source": "project", "tools": reviewer_line}
    if spec.denies:
        reviewer["disallowedTools"] = list(REVIEWER_DENIED)
    return {
        "reviewer": reviewer,
        "claude-implementer": {"model": "sonnet", "effort": "medium", "maxTurns": 60,
                               "experimental.cacheTtl": "1h", "description": "str(120)", "source": "project",
                               "tools": list(IMPLEMENTER_LINE)},
    }


def _snapshot(spec: Spec, ts: datetime, agents: dict, model: str, *, slug: str = SLUG,
              project_model: str | None = None) -> dict:
    from claudeglass.snapshots import snapshot_project_key

    effective_agents = {name: {k: v for k, v in entry.items() if k != "tools"} for name, entry in agents.items()}
    snap = {
        "schema": 2, "ts": ts.strftime("%Y%m%dT%H%M%SZ"), "session_id": None, "claude_version": spec.version,
        "cwd_hash": "sha256:" + "a" * 64, "project_slug": snapshot_project_key(slug),
        "user_settings": {"model": model, "effortLevel": "high", "permissions": "dict(3)"},
        "project_settings": {"hooks": "dict(2)"},
        "settings_layers": {"user": {"model": model, "effortLevel": "high"}, "project_shared": {},
                            "project_local": {}, "managed": {}},
        "effective": {"model": model, "effortLevel": "high", "hooks": "dict(2)"},
        "effective_agents": effective_agents,
        "mcp_servers": {"names": ["figma"], "enabled_mcpjson_servers": ["figma"], "disabled_mcpjson_servers": []},
        "enabled_plugins": [], "agents": agents, "env_names": ["CLAUDE_CODE_VERSION"],
        "content_layers": {"claude_md": {"user_bytes": 2000, "project_root_bytes": 118000, "project_local_bytes": 0,
                                         "present": True},
                           "rules": {"bytes": 16000, "unscoped_bytes": 4000}, "agents_summary": {"count": len(agents)}},
        "content_hash": "sha256:" + "2" * 64 if model == "opus" else "sha256:" + "3" * 64,
    }
    if spec.second_project:
        # The project's own settings (``project_model``) win over your user settings.
        effective_model = project_model or model
        snap["effective"]["model"] = effective_model
        snap["effective_provenance"] = {"model": "project_local" if project_model else "user", "effortLevel": "user"}
        if project_model:
            snap["project_settings"]["model"] = project_model
            snap["settings_layers"]["project_local"] = {"model": project_model}
        snap["content_hash"] = "sha256:" + ("2" if effective_model == "opus" else "3") * 64
    return snap


def write_config(spec: Spec, root: Path, sessions: list[dict]) -> dict:
    cfg = root / "claudeglass"
    (cfg / "snapshots").mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text(f'billing = "{spec.billing}"\n', encoding="utf-8")
    agents = _agent_files(spec)
    snaps = [(START - timedelta(days=1), agents, "opus")]
    if spec.second_project:
        # (when, project, your user settings' model, the project's own model)
        day = timedelta(days=1)
        two = [(START - day, SLUG, "opus", None), (START - day, SLUG_B, "opus", None)]
        if spec.model_back:
            two += [(START + 2 * day, SLUG, "opus", "sonnet"), (START + 5 * day, SLUG, "opus", None)]
        else:
            two += [(START + 4 * day, SLUG, "opus", "sonnet")]
        two.append((START + 7 * day + timedelta(hours=1), SLUG_B, "opus", None))
        if spec.a_newest:
            two.append((START + 7 * day + timedelta(hours=2), SLUG, "opus", "sonnet"))
        for ts, slug, model, project_model in two:
            snap = _snapshot(spec, ts, agents, model, slug=slug, project_model=project_model)
            name = f"{snap['ts']}-{'a' if slug == SLUG else 'b'}.json"
            (cfg / "snapshots" / name).write_text(json.dumps(snap), encoding="utf-8")
        return agents
    if spec.changed_later:
        # Later, the reviewer's line loses Edit (which most of its spawns called)
        # and the main model is set to Sonnet.
        shorter = ", ".join(tool for tool in REVIEWER_LINE.split(", ") if tool != "Edit")
        snaps = [(START - timedelta(days=1), agents, "sonnet"), (START + timedelta(days=3), agents, "opus"),
                 (START + timedelta(days=5), _agent_files(spec, reviewer_line=shorter), "opus")]
    for ts, files, model in snaps:
        snap = _snapshot(spec, ts, files, model)
        (cfg / "snapshots" / f"{snap['ts']}.json").write_text(json.dumps(snap), encoding="utf-8")
    if spec.statusline:
        header = ["logged_at", "session_id", "window", "used_percentage", "resets_at", "source",
                  "context_window_used_tokens", "context_window_size", "context_window_cache_read_tokens", "cache_warm",
                  "cache_ttl_s", "cache_expires_in_s", "cache_misses", "cache_last_miss_cause",
                  "cache_recache_tokens_if_cold", "cache_miss_causes"]
        with open(cfg / "usage-log.csv", "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for n, s in enumerate(sessions):
                for k in range(4):
                    at = (s["end"] - timedelta(minutes=30 - k * 8)).isoformat().replace("+00:00", "Z")
                    used = 60_000 + k * 25_000 + n * 3000
                    w.writerow([at, s["sid"], "context_window", round(used / 2000, 1), "", "statusline", used, 200_000,
                                used - 4000, 1, 300, 120, k % 2, "ttl" if k % 2 else "", 8000 * (k % 2),
                                "ttl:1" if k % 2 else ""])
                at = s["end"].isoformat().replace("+00:00", "Z")
                reset = (s["end"] + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
                w.writerow([at, s["sid"], "five_hour", 20 + n * 3, reset, "statusline"] + [""] * 10)
                w.writerow([at, s["sid"], "seven_day", 10 + n * 2, reset, "statusline"] + [""] * 10)
    return agents


def build(spec: Spec, root: Path) -> Profile:
    """Write ``spec``'s projects folder and config folder under ``root`` (replacing what is there)."""
    if root.exists():
        shutil.rmtree(root)
    counts: dict[str, int] = {}
    sessions = [main_session(spec, root, i, counts) for i in range(spec.sessions)]
    if spec.second_project:
        counts_b: dict[str, int] = {}
        for i in range(spec.sessions):
            main_session(spec, root, i, counts_b, slug=SLUG_B, sid_prefix="sesb")
    if spec.workflows:
        first = 0
        for index in (2, 4, 5, 7):
            if index < len(sessions):
                clock = Clock(sessions[index]["end"] - timedelta(minutes=20))
                first = write_workflow(spec, root, sessions[index]["sid"], index, clock, first)
    agents = write_config(spec, root, sessions)
    return Profile(spec=spec, root=root, agent_files=agents)


# -- built once per test run -------------------------------------------------

_BUILT: dict = {}


def profile_inputs(name: str, tmp_path_factory):
    """The profile ``name`` written under a temp folder and read by the
    product's own report path (``agreement.load_inputs``), once per test run:
    ``(profile, inputs)``. The cards the spec ignores are ignored through
    ``ignores`` in the profile's config folder first, as the dashboard stores
    them, and the inputs read back with them skipped."""
    if name not in _BUILT:
        import agreement
        from claudeglass import ignores

        spec = SPECS[name]
        profile = build(spec, tmp_path_factory.mktemp(f"agree_{name}"))
        project = None if spec.second_project else SLUG
        inputs = agreement.load_inputs(profile.projects_root, profile.config_dir, project)
        if spec.ignore_ids:
            wanted = [rec for rec in inputs.recs if rec.id in spec.ignore_ids]
            ignores.set_ignored(profile.config_dir, wanted, ignored=True, project=project)
            inputs = agreement.load_inputs(profile.projects_root, profile.config_dir, project)
        _BUILT[name] = (profile, inputs)
    return _BUILT[name]

"""Agreement checks: places in ClaudeGlass that should say the same thing,
compared with each other.

Most defects found on the card-accuracy branch were two places that should
agree and did not: a card and its check, a card's text and its fix, the
headline and the cards under it. A test per defect catches that case. These
checks catch the next one. Each is a plain function of what the product
already builds for one project and window (an :class:`Inputs`), and returns
a list of sentences, one per disagreement; an empty list means the two sides
agree. ``tests/test_agreement.py`` runs all seven over eight synthetic
profiles (``agreement_profiles.py``); a runner outside the repo runs the same
functions over a real corpus.

Every function gets both sides from the product's own code (the
recommendations, ``quick_actions``, ``fixes.fix_file``, ``fixes.project_prompt``,
the dashboard's JavaScript run through Node, ``change_points``), never from a
second copy of a rule. What a check adds is the comparison. The sentences hold
card ids, agent names, tool names and counts, never transcript text, so a
result is safe to print for private data.

The seven (numbers as in the plan's "Agreement checks"):

1. A tools card's suggested line keeps every tool the agent called in at
   least the bar's share of its spawns (and, for workflow agents, every
   writing or shell tool any of them called), unless the agent's own line
   already lacks it.
2. Where the action text of a card whose fix is one exact file names an
   agents folder, it is the folder of that card's fix (``fixes.fix_file``).
3. The Overview's headline saving and the habits check's saving are built
   only from the levers and the action and advice cards: an info, fixed or
   ignored card adds nothing.
4. Each check's figures equal its cards': the Tools check's "rarely called"
   count and tokens are the tools card's for the same agent, and an ignored
   card adds no fix, tip or mention to any check.
5. The project bundle's counts add up, every entry in ``files`` is one
   concrete file with a one-file fix under it, and the counts do not depend
   on the order of the cards.
6. Every hint that generated text names as live is in the derived live list.
7. A card and a check about the same setting give the same advice, and a card
   and the Changes page state the same current value.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from claudeglass import (
    capture_catalogue,
    capture_view,
    change_points,
    cli,
    context_budget,
    fixes,
    ignores,
    quick_actions,
    recommend,
    snapshots,
)
from claudeglass.render.json_out import to_jsonable
from claudeglass.units import Units

#: The severities whose cards count toward a saving or a bundle.
COUNTING = ("action", "advice")


class CheckUnavailable(Exception):
    """A check could not run here (Node is not installed): the runner says
    so rather than counting it as agreement."""


# -- what the product builds -------------------------------------------------


@dataclass
class Inputs:
    """What the product builds for one project and window: the report, its
    recommendations, the quick-action context, the stored ignores."""

    model: object
    ctx: quick_actions.Context
    config_dir: Path
    #: The folders (as ``~/.claude/projects`` names them) the report read.
    projects: tuple[str, ...] = ()
    #: Read in the all-projects view (``load_inputs`` with no project).
    all_projects: bool = False
    _results: dict = field(default_factory=dict)

    @property
    def recs(self) -> list:
        return list(self.model.recommendations)

    @property
    def skip(self) -> frozenset:
        return self.ctx.skip_keys

    @property
    def agents(self) -> dict:
        return self.ctx.agents

    def result(self, check_id: str) -> dict:
        """One quick-action check in full, run once."""
        if check_id not in self._results:
            self._results[check_id] = quick_actions.run(check_id, self.ctx)
        return self._results[check_id]

    def with_skip(self, keys) -> "Inputs":
        """The same report with ``keys`` the ignored cards (not written anywhere)."""
        ctx = dataclasses.replace(self.ctx, skip_keys=frozenset(keys))
        return Inputs(model=self.model, ctx=ctx, config_dir=self.config_dir, projects=self.projects,
                      all_projects=self.all_projects)

    def with_recommendations(self, recs) -> "Inputs":
        model = _copy_model(self.model)
        model.recommendations = list(recs)
        return Inputs(model=model, ctx=dataclasses.replace(self.ctx, model=model), config_dir=self.config_dir,
                      projects=self.projects, all_projects=self.all_projects)


def _copy_model(model):
    import copy

    return copy.copy(model)


def load_inputs(
    projects_root, config_dir, project=None, *, days: int | None = None, claude_root=None
) -> Inputs:
    """The report and the quick-action context for ``project`` (a folder name
    under ``projects_root``; every project when ``None``), built by the CLI's
    own report path (``cli._cmd_report_like``) and the context the dashboard
    gives the checks (``api._quick_context``): the settings in force and
    every project's agents come from one merged snapshot, the ignored cards
    from ``ignores``. ``days`` is a window of the last N days; ``None`` is all time.
    ``projects_root`` is the folder the report reads and the CLAUDE.md checks read
    with it, wherever the config folder is (a copy kept apart from the projects, as
    in a container, answers as one beside them); ``claude_root`` is Claude's own
    folder when the config folder's parent is not it."""
    argv = ["check", "--projects-root", str(projects_root), "--config-dir", str(config_dir), "--no-cache", "--quiet"]
    argv += ["--claude-root", str(claude_root)] if claude_root else []
    argv += ["--days", str(days)] if days else ["--since", "2000-01-01"]
    argv += ["--project", project] if project else ["--all-projects"]
    args = cli._make_parser().parse_args(argv)
    box: dict = {}

    def emit(model, config_dir, window, since_dt, until_dt, projects):
        box.update(model=model, window=window, since=since_dt, until=until_dt, projects=tuple(projects))
        return 0

    code = cli._cmd_report_like(args, None, emit=emit)
    if code != 0 or "model" not in box:
        raise RuntimeError(f"the report could not be built (exit {code}): no sessions under the projects folder?")
    model, config_dir = box["model"], Path(config_dir)
    read_claude_root, read_roots = cli._read_where(args, config_dir)
    snaps = snapshots.load_snapshots(config_dir)
    if project:
        snaps = snapshots.snapshots_for_projects(snaps, box["projects"])
    snapshot = snapshots.with_every_project_agents(snaps, snapshots.canonical_project_keys(box["projects"]))
    effective, effective_agents, agent_files = {}, {}, {}
    if snapshot is not None:
        effective = snapshots.effective_config_in_force(snapshot)
        found = snapshot.data.get("effective_agents")
        effective_agents = found if isinstance(found, dict) else {}
        files = snapshot.data.get("agents")
        agent_files = files if isinstance(files, dict) else {}
    ctx = quick_actions.Context(
        model=model,
        units=model.units or Units(),
        period=cli._period_phrase(box["window"]),
        config_dir=config_dir,
        effective=effective,
        effective_agents=effective_agents,
        agents=agent_files,
        skip_keys=ignores.skip_keys(config_dir, model.recommendations, project),
        only=cli._report_only(args, box["projects"], config_dir),
        since_ts=box["since"].timestamp() if box["since"] else None,
        until_ts=box["until"].timestamp() if box["until"] else None,
        claude_root=read_claude_root,
        projects_roots=tuple(read_roots),
    )
    return Inputs(model=model, ctx=ctx, config_dir=config_dir, projects=box["projects"], all_projects=project is None)


def tables_of(model) -> dict[str, list[dict]]:
    """Every report table as a list of dicts by column key, by table name."""
    out: dict[str, list[dict]] = {}
    for section in getattr(model, "sections", None) or ():
        for table in section.tables:
            keys = [column.key for column in table.columns]
            out[table.name] = [dict(zip(keys, row)) for row in table.rows]
    return out


def _names(text) -> list[str]:
    return [name.strip() for name in str(text or "").split(",") if name.strip()]


def _open(rec, skip) -> bool:
    """A card that is on offer: not ignored, not already fixed, not information only."""
    return rec.key not in skip and rec.variant != "fixed" and rec.severity in COUNTING


def _who(rec) -> str:
    return f"{rec.key or rec.id}"


# -- 1. a tools card keeps what the agent calls -------------------------------

_LINE_IN_ACTION = re.compile(r"(?:sets this line|Limit .+?'s tools to the ones it calls): tools: (.+?)\. Rarely used")


def _suggested_line(rec) -> list[str] | None:
    """The tools line a tools card suggests, from its change, or (a workflow
    card, which changes no setting) from the line its action quotes."""
    for change in rec.changes:
        if change.key == "tools" and isinstance(change.value, (list, tuple)):
            return [str(entry) for entry in change.value]
    found = _LINE_IN_ACTION.search(rec.action or "")
    return _names(found.group(1)) if found else None


def check_tools_lines(inp: Inputs) -> list[str]:
    """Check 1. For each ``spawn-tools-list`` card: every tool its agent
    called in at least :data:`context_budget.REMOVABLE_USE_SHARE` of the
    spawns it was offered to is on the suggested line, unless the agent's own
    ``tools:`` line already lacks it or its ``disallowedTools:`` line withholds it; every writing or
    shell tool any run of that agent called is on it too. The tools called are
    the report's own (``agent_startup_diet`` and ``agent_startup_tools``)."""
    problems: list[str] = []
    tables = tables_of(inp.model)
    diet = {row["agent_type"]: row for row in tables.get("agent_startup_diet", [])}
    rarely = tables.get("agent_startup_tools", [])
    bar = context_budget.REMOVABLE_USE_SHARE
    for rec in inp.recs:
        if rec.id != "spawn-tools-list":
            continue
        agent = rec.agent_type
        workflow = rec.variant == "workflow-script"
        line = _suggested_line(rec)
        if line is None:
            problems.append(f"{_who(rec)}: no tools line could be read from the card")
            continue
        row = diet.get(agent)
        if row is None:
            problems.append(f"{_who(rec)}: the report has no agent_startup_diet row for {agent}")
            continue
        called = {name: "called in at least the bar's share of its spawns" for name in _names(row.get("keep_tools"))}
        for name in _names(row.get("rule_kept_tools")):
            called.setdefault(name, "called by a run of this agent")
        for tool in rarely:
            if tool.get("agent_type") != agent:
                continue
            used, offered = tool.get("used_spawns") or 0, tool.get("offered_spawns") or 0
            if offered and used / offered >= bar:
                called.setdefault(tool["tool"], f"called in {used} of {offered} spawns")
            elif used and tool["tool"] in context_budget._WRITER_TOOLS:
                called.setdefault(tool["tool"], f"called by a run of this agent in {used} of {offered} spawns")
        file = inp.agents.get(agent) or {}
        own = recommend.read_agent_tools(file.get("tools"), file.get("disallowedTools")) if not workflow else None
        for name, why in sorted(called.items()):
            if own is not None and not own.offered(name):
                continue
            if not any(recommend._entry_covers(entry, name) for entry in line):
                problems.append(
                    f"{_who(rec)}: the suggested line leaves out {name}, {why} "
                    f"(line: {', '.join(line)})"
                )
    return problems


# -- 2. a card's agents folder is its fix's folder ----------------------------

_AGENTS_FOLDER = re.compile(r"(?:~/)?\.claude/agents/")


def _folders(text) -> set[str]:
    return set(_AGENTS_FOLDER.findall(str(text or "")))


def check_agent_folders(inp: Inputs) -> list[str]:
    """Check 2. Where a card names an agents folder (``~/.claude/agents/`` or
    ``.claude/agents/``) in its action text or its fix's prompt, it is a folder
    the card's fix works in (``fixes.fix_file`` for each of its changes). A card
    that sets a standing rule (kind ``rule`` in ``fixes.fix_group``) may name the
    agent's own file as a second place, so its text is not held to that; every
    other card is, one exact file, an "A or B" pair or several files. Two cards
    for one agent also name one folder between them."""
    problems: list[str] = []
    single: dict[str, dict[str, str]] = {}
    for rec in inp.recs:
        files = {fixes.fix_file(rec, change) for change in (rec.changes or [None])}
        allowed = set().union(*(_folders(f) for f in files if f))
        standing_rule = any(fixes.fix_group(rec, change)[1] == "rule" for change in (rec.changes or [None]))
        texts = {"action": rec.action, **{f"prompt {i + 1}": fix.get("prompt") for i, fix in enumerate(rec.fixes)}}
        for where, text in () if standing_rule else texts.items():
            for folder in sorted(_folders(text) - allowed):
                problems.append(
                    f"{_who(rec)}: the {where} names {folder} but the fix works in "
                    f"{' / '.join(sorted(str(f) for f in files))}"
                )
        # A fix with one exact file (not an "A or B" pair) puts that agent there.
        for file in files:
            if file and ".claude/agents/" in file and " or " not in file and rec.agent_type:
                single.setdefault(rec.agent_type, {})[_AGENTS_FOLDER.search(file).group(0)] = _who(rec)
    for agent, folders in sorted(single.items()):
        if len(folders) > 1:
            problems.append(f"{agent}: cards put its file in different folders: " + "; ".join(
                f"{folder} ({card})" for folder, card in sorted(folders.items())
            ))
    return problems


# -- 3. totals count only what is on offer ------------------------------------

#: What the Overview's headline runs on, from the dashboard's own modules.
_OVERVIEW_FUNCTIONS = (
    ("charts-types.js", "tableObjects"),
    ("page-actions.js", "areaOf"),
    ("page-actions.js", "groupRecommendations"),
    ("page-actions.js", "groupSavingUsd"),
    ("page-overview.js", "usdOf"),
    ("page-overview.js", "combinedSaving"),
    ("page-overview.js", "agentModelItems"),
    ("page-overview.js", "availableSaving"),
)


def _declaration(module: str, name: str) -> str:
    from test_service_static import _declaration_source, _static_text

    source = _declaration_source(_static_text(module), name)
    return re.sub(r"^export\s+", "", source) + ";\n"


def run_node(functions: list[tuple[str, str]], expression: str, preamble: str = "") -> object:
    """Dashboard functions run in Node, as the static tests do: ``preamble``,
    each (module, name) function's source, then ``expression``, whose value
    comes back through JSON."""
    from test_service_static import _function_source, _static_text

    node = shutil.which("node")
    if node is None:
        raise CheckUnavailable("Node is not installed")
    script = preamble + "\n".join(_function_source(_static_text(module), name) for module, name in functions)
    script += f"\nprocess.stdout.write(JSON.stringify({expression}));"
    # Through a file: the rows of a big project are longer than Windows lets a command line be.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "agreement.js"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run(
            [node, str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=True
        )
    return json.loads(done.stdout)


def overview_saving(inp: Inputs, recs: list, skip) -> float:
    """The Overview's headline saving over ``recs`` (the ones in ``skip``
    marked ignored, as ``/api/recommendations`` marks them), by the page's own
    ``availableSaving`` and ``groupRecommendations`` run in Node."""
    rows = []
    for rec in recs:
        row = to_jsonable(rec)
        row["ignored"] = rec.key in skip
        rows.append(row)
    preamble = _declaration("page-overview.js", "LEVER_RULES") + _declaration("page-overview.js", "AGENT_MODEL_RULES")
    preamble += _declaration("page-actions.js", "RULE_AREA") + _declaration("ui.js", "SEVERITY_ORDER")
    preamble += 'var MAIN_AGENT = "top-level";\nvar WORKFLOW_AGENT = "workflow-subagent";\n'
    names = {"model_swap_by_agent_type"}
    tables = {t["name"]: t for t in (to_jsonable(t) for s in inp.model.sections for t in s.tables) if t["name"] in names}
    expression = f"availableSaving([], groupRecommendations({json.dumps(rows)}), {json.dumps(tables)})"
    return float(run_node(list(_OVERVIEW_FUNCTIONS), expression, preamble=preamble))


def check_totals(inp: Inputs) -> list[str]:
    """Check 3. A saving computed over every card equals the same saving
    computed over only the action and advice cards that are neither ignored
    nor fixed: an info, fixed or ignored card adds nothing. Run for the
    Overview's headline (the page's JavaScript) and the habits check's saving."""
    problems: list[str] = []
    recs, skip = inp.recs, inp.skip
    counted = [rec for rec in recs if _open(rec, skip)]
    extra = [rec for rec in recs if not _open(rec, skip)]
    names = ", ".join(sorted(f"{rec.key or rec.id}" for rec in extra if (rec.saving_usd or 0) > 0))
    everything = overview_saving(inp, recs, skip)
    only = overview_saving(inp, counted, frozenset())
    if abs(everything - only) > 1e-6:
        problems.append(
            f"the Overview's headline saving is {everything:.2f} USD with every card and {only:.2f} USD with only "
            f"the open action and advice cards; cards that add to it: {names or 'none with a saving'}"
        )
    # The habits check's saving is built from the recommendations of its own rules.
    with_all = inp.with_skip(skip)
    without = inp.with_recommendations(counted).with_skip(frozenset())
    got, want = with_all.result("habits").get("saving_usd") or 0.0, without.result("habits").get("saving_usd") or 0.0
    if abs(got - want) > 1e-6:
        problems.append(f"the habits check saves {got:.2f} USD with every card and {want:.2f} USD with only the open ones")
    return problems


# -- 4. a check's figures are its cards' --------------------------------------

_RARE_LIST = re.compile(r"fewer than \d+% of the spawns offered it\): (.+?)\. Claude Code adds")
_MORE = re.compile(r"^and (\d+) more$")
_CHECK_RARE = re.compile(r"^(\d+) \(about ([\d,]+) tokens\)$")
_CARD_TOKENS = re.compile(r"takes about ([\d,]+) tokens out of each start")


def _card_rare(rec) -> tuple[int, int] | None:
    """How many tools a tools card says are rarely called, and the tokens it
    says leaving them out takes from each start, as its text words them."""
    found, tokens = _RARE_LIST.search(rec.action or ""), _CARD_TOKENS.search(rec.why or "")
    if not found or not tokens:
        return None
    parts = [part.strip() for part in found.group(1).split(", ")]
    count = len(parts)
    if parts and (more := _MORE.match(parts[-1])):
        count = len(parts) - 1 + int(more.group(1))
    return count, int(tokens.group(1).replace(",", ""))


def _set_tokens(command) -> set[str]:
    """The ``key=value`` words a dry-run command sets."""
    if not command:
        return set()
    try:
        words = shlex.split(str(command))
    except ValueError:
        words = str(command).split()
    return {words[i + 1] for i, word in enumerate(words[:-1]) if word == "--set"}


def _same_change(rec, fix: dict) -> bool:
    """Whether ``fix`` (a check's) carries a change ``rec`` (a card) proposes:
    the same setting for the same agent set to the same value (or, where
    neither fix has a command to run, with the same prompt); a card that
    changes no setting is matched by its fix's prompt."""
    if rec.changes:
        tokens = _set_tokens(fix.get("command"))
        for change in rec.changes:
            if fix.get("key") != change.key or (fix.get("agent") or None) != (change.agent or None):
                continue
            for own in rec.fixes:
                if own.get("key") == change.key and (own.get("agent") or None) == (change.agent or None):
                    mine = _set_tokens(own.get("command"))
                    if mine & tokens or (not mine and not tokens and own.get("prompt") == fix.get("prompt")):
                        return True
        return False
    prompts = {fix_.get("prompt") for fix_ in rec.fixes if fix_.get("prompt")}
    return bool(fix.get("prompt")) and fix["prompt"] in prompts


#: The most advice and action cards tried as ignored, per check.
_IGNORE_TRIES = 8


def check_tools_figures(inp: Inputs) -> list[str]:
    """Check 4, first half. The Tools check's "rarely called" count and tokens
    for an agent equal its tools card's."""
    problems: list[str] = []
    tools = inp.result("tools")
    rows = {str(row[0]): row for row in ((tools.get("table") or {}).get("rows") or [])}
    for rec in inp.recs:
        if rec.id != "spawn-tools-list":
            continue
        card = _card_rare(rec)
        row = rows.get(str(rec.agent_type))
        if card is None:
            problems.append(f"{_who(rec)}: the card's text does not give its rarely-called tools and tokens")
        elif row is None:
            problems.append(f"{_who(rec)}: the Tools check has no row for {rec.agent_type}")
        else:
            shown = _CHECK_RARE.match(str(row[4]))
            if not shown:
                problems.append(f"{_who(rec)}: the Tools check says {row[4]!r} for {rec.agent_type}, the card says "
                                f"{card[0]} tools and {card[1]:,} tokens")
            elif (int(shown.group(1)), int(shown.group(2).replace(",", ""))) != card:
                problems.append(
                    f"{_who(rec)}: the Tools check counts {shown.group(1)} rarely called tools "
                    f"(about {shown.group(2)} tokens) for {rec.agent_type}, the card {card[0]} (about {card[1]:,})"
                )
    return problems


def check_ignored_adds_nothing(inp: Inputs) -> list[str]:
    """Check 4, second half. With any one open card ignored, no check still
    offers that card's fix, lists it as a tip or names its title in its
    summary. Tried for up to :data:`_IGNORE_TRIES` open cards of each check's
    own rules."""
    problems: list[str] = []
    open_cards = [rec for rec in inp.recs if _open(rec, inp.skip)]
    for check in quick_actions.CHECKS:
        claimed = [rec for rec in open_cards if rec.id in check.rule_ids][:_IGNORE_TRIES]
        for rec in claimed:
            result = inp.with_skip(set(inp.skip) | {rec.key}).result(check.id)
            if any(_same_change(rec, fix) for fix in result["fixes"]):
                problems.append(f"{check.id}: still offers the fix of {_who(rec)} with that card ignored")
            if rec.title in {tip.get("title") for tip in result["tips"]} or any(
                tip.get("text") and tip["text"] in (rec.action, rec.why) for tip in result["tips"]
            ):
                problems.append(f"{check.id}: still lists {_who(rec)} as a tip with that card ignored")
            if rec.title and rec.title.rstrip(".") in str(result["summary"]):
                problems.append(f"{check.id}: its summary still names {_who(rec)} with that card ignored")
    return problems


def check_figures(inp: Inputs) -> list[str]:
    """Check 4. Each check's figures equal its cards': the Tools check's
    count and tokens are the tools card's (:func:`check_tools_figures`), and an
    ignored card adds nothing to any check (:func:`check_ignored_adds_nothing`)."""
    return check_tools_figures(inp) + check_ignored_adds_nothing(inp)


# -- 5. the bundle's counts add up --------------------------------------------

_PATH_ALTERNATIVE = re.compile(r"[~\w./{}\-]+")


def _is_one_file(label: str) -> bool:
    """A header that is a path, or an "A or B" pair of paths: no spaces, and a
    folder or an extension in each."""
    return all(_PATH_ALTERNATIVE.fullmatch(alt) and ("/" in alt or "." in alt) for alt in label.split(" or "))


def _prompt_fixes(rec) -> list[dict]:
    """The fixes of ``rec`` that carry a prompt and belong to one of its changes."""
    out = []
    for fix in rec.fixes:
        if not fix.get("prompt"):
            continue
        if rec.changes and not any(
            fix.get("key") == c.key and (fix.get("agent") or "") == (c.agent or "") for c in rec.changes
        ):
            continue
        out.append(fix)
    return out


def check_bundle(inp: Inputs) -> list[str]:
    """Check 5. ``fixes.project_prompt`` over the project's cards: the fixed
    and info cards it leaves out are exactly the fixed and info cards, the
    cards it uses are no more than the cards with a prompt to give and, with
    the ones left out for being unconfirmed or managed or to try first, no fewer; an empty
    prompt means no cards and a prompt means some; every ``files`` entry is
    one concrete file that the prompt has a heading for and has a one-file fix
    under it; the same cards in the opposite order give the same counts; and no
    ignored card's prompt is in it."""
    problems: list[str] = []
    recs, skip = inp.recs, set(inp.skip)
    data = fixes.project_prompt(recs, skip)
    prompt, left = data["prompt"], data["left_out"]
    on_offer = [rec for rec in recs if rec.key not in skip]
    fixed = sum(1 for rec in on_offer if rec.variant == "fixed")
    info = sum(1 for rec in on_offer if rec.variant != "fixed" and rec.severity == "info")
    if left["fixed"] != fixed:
        problems.append(f"the bundle leaves out {left['fixed']} fixed cards, the project has {fixed}")
    if left["info"] != info:
        problems.append(f"the bundle leaves out {left['info']} information cards, the project has {info}")
    live = [rec for rec in on_offer if rec.variant != "fixed" and rec.severity != "info" and _prompt_fixes(rec)]
    if data["cards"] > len(live):
        problems.append(f"the bundle uses {data['cards']} cards, only {len(live)} have a prompt to give")
    if len(live) > data["cards"] + left["unconfirmed"] + left["managed"] + left["trial"]:
        problems.append(
            f"{len(live)} cards have a prompt to give but the bundle uses {data['cards']} and leaves out "
            f"{left['unconfirmed']} unconfirmed and {left['managed']} managed and {left['trial']} to try first"
        )
    if bool(prompt) != bool(data["cards"]):
        problems.append(f"the bundle has {data['cards']} cards and {'a' if prompt else 'no'} prompt")
    in_files = sum(entry["fixes"] for entry in data["files"])
    if data["cards"] > in_files + data["several"] + data["other"]:
        problems.append(
            f"the bundle uses {data['cards']} cards but holds only {in_files + data['several'] + data['other']} fixes"
        )
    kinds: dict[str, set[str]] = {}
    for rec in recs:
        for change in rec.changes or [None]:
            header, kind = fixes.fix_group(rec, change)
            if header is not None:
                kinds.setdefault(header, set()).add(kind)
    for entry in data["files"]:
        header = entry["file"]
        if not _is_one_file(header):
            problems.append(f"the bundle counts {header!r} as one file")
        elif "file" not in kinds.get(header, {"file"}):
            problems.append(f"the bundle counts {header!r} as a file, no fix under it groups as one: {sorted(kinds[header])}")
        if f"## {header}\n" not in prompt:
            problems.append(f"the bundle lists {header!r} but its prompt has no such heading")
        if entry["fixes"] < 1:
            problems.append(f"the bundle lists {header!r} with {entry['fixes']} fixes")
    # The counts are per fix: the same cards in the opposite order give the same counts.
    flipped = fixes.project_prompt(list(reversed(recs)), skip)

    def counts(result):
        return sorted((f["file"], f["fixes"]) for f in result["files"]), result["several"], result["other"], result["cards"]

    if counts(flipped) != counts(data):
        problems.append(
            f"the bundle's counts depend on the order of the cards: {counts(data)[:3]} one way, "
            f"{counts(flipped)[:3]} the other"
        )
    open_bodies = {fixes._bundle_body(fix["prompt"])[0] for rec in on_offer for fix in _prompt_fixes(rec)}
    for rec in recs:
        if rec.key in skip:
            for fix in _prompt_fixes(rec):
                body = fixes._bundle_body(fix["prompt"])[0]
                if body in prompt and body not in open_bodies:
                    problems.append(f"the bundle holds the prompt of {_who(rec)}, which is ignored")
    return problems


# -- 6. generated text names only live hints ----------------------------------

#: How generated prose names each coaching hint, lower case (the hint's title
#: from the catalogue is added). A new hint has no entry until someone writes
#: one: ``test_agreement`` fails for it, so a check never skips a hint it
#: cannot recognise.
#: "web result" and "approving a plan" are left out: the big_output metric and the plan check use them for themselves.
HINT_WORDS: dict[str, tuple[str, ...]] = {
    "plan_fresh": ("plan approved", "building a plan"),
    "plan_fresh_early": ("plan approved", "clear context"),
    "split_run": ("ended too big", "smaller pieces of work", "very long brief"),
    "quiet_output": ("large read", "saved output"),
    "report_reread": ("starting background agents", "background agents"),
    "drip_feed": ("small requests", "one at a time"),
    "big_paste": ("huge paste", "big paste", "pasting a lot"),
    "status_poll": ("how background work is going", "background work"),
    "cold_return": ("after a break", "coming back after", "outlasted the prompt cache"),
}


def live_hints() -> list[str]:
    """The coaching hints that speak: those of ``COACHING_HINTS`` that are not
    switched off (``LIVE_OFF_HINTS``) and that something in the hook still
    raises (not ``NO_TRIGGER_HINTS``). A derived list from the catalogue, once
    it has one."""
    derived = getattr(capture_catalogue, "live_coaching_hints", None)
    if callable(derived):
        return list(derived())
    skip = set(capture_catalogue.LIVE_OFF_HINTS) | set(capture_catalogue.NO_TRIGGER_HINTS)
    return [hint for hint in capture_catalogue.COACHING_HINTS if hint not in skip]


def generated_texts() -> dict[str, str]:
    """The prose the product generates about coaching hints."""
    metric = capture_catalogue.METRICS_BY_ID["coaching_notes"]
    markdown = capture_catalogue.render_markdown()
    return {
        "coaching_notes.what": metric.what,
        "coaching_notes.why": metric.why,
        "capture_view.COACHING_NOTES_ON": capture_view.COACHING_NOTES_ON,
        "docs/capture.md (generated)": markdown,
    }


def hint_words(hint: str) -> tuple[str, ...]:
    titles = {**capture_catalogue.TIP_HINT_TITLES, **capture_catalogue.CLAUDE_ONLY_HINT_TITLES}
    return tuple(word.lower() for word in (*HINT_WORDS.get(hint, ()), titles.get(hint, "")) if word)


def check_live_hints(texts: dict[str, str] | None = None, live: list[str] | None = None) -> list[str]:
    """Check 6. A hint that generated text names (by its title or the words
    :data:`HINT_WORDS` gives it) is in the live list. The text in
    ``texts`` (default: what the product generates) is read whole, so the
    generated ``docs/capture.md`` is held to it too."""
    texts = generated_texts() if texts is None else texts
    live = live_hints() if live is None else live
    problems = []
    for where, text in sorted(texts.items()):
        lower = text.lower()
        for hint in capture_catalogue.COACHING_HINTS:
            if hint in live:
                continue
            named = [word for word in hint_words(hint) if word in lower]
            if named:
                problems.append(f"{where} names {hint} ({named[0]!r}), which is not live")
    return problems


# -- 7. a card and a check give the same advice -------------------------------


def _agent_of(item) -> str | None:
    return item.get("agent") or None if isinstance(item, dict) else getattr(item, "agent", None) or None


def _norm(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def check_same_setting(inp: Inputs) -> list[str]:
    """Check 7. (a) Where an open card and a check's fix change the same
    setting for the same agent, they set it to the same value, and a check
    that says nothing needs changing is not set against an open card for one
    of its rules that changes a setting. (b) A card's current value for a
    setting is the one the Changes page last shows it moved to: in one
    project's view, by a change there or for every project; in the
    all-projects view, a main-session setting by a change for every project
    only (a project's own files set it there alone) and an agent's by either."""
    problems: list[str] = []
    open_cards = [rec for rec in inp.recs if _open(rec, inp.skip) and rec.changes]
    card_values: dict[tuple, dict[str, str]] = {}
    for rec in open_cards:
        for fix in rec.fixes:
            for token in _set_tokens(fix.get("command")):
                card_values.setdefault((fix.get("key"), _agent_of(fix)), {})[token] = _who(rec)
    for check in quick_actions.CHECKS:
        result = inp.result(check.id)
        seen: dict[tuple, set[str]] = {}
        for fix in result["fixes"]:
            seen.setdefault((fix.get("key"), _agent_of(fix)), set()).update(_set_tokens(fix.get("command")))
        for key, tokens in sorted(seen.items(), key=str):
            theirs = card_values.get(key)
            if theirs and tokens and not tokens <= set(theirs):
                problems.append(
                    f"{check.id} sets {key[0]}{' for ' + key[1] if key[1] else ''} to "
                    f"{', '.join(sorted(tokens))}, the card {', '.join(sorted(set(theirs.values())))} to "
                    f"{', '.join(sorted(theirs))}"
                )
        if result["status"] != "act":
            for rec in open_cards:
                if rec.id in check.rule_ids and rec.saving_usd:
                    problems.append(
                        f"{check.id} says {result['status']} but the open card {_who(rec)} changes "
                        f"{', '.join(c.key for c in rec.changes)}"
                    )
    keys = {key for slug in inp.projects for key in snapshots.snapshot_project_keys(slug)}
    # In the all-projects view a main-session setting one project's own files changed holds there alone
    # (``snapshots.with_every_project_agents`` reads your user settings); agent files are merged per
    # project, so their changes still count.
    points = [p for p in change_points.change_points(inp.config_dir) if not p.project or p.project in keys]
    for rec in open_cards:
        for change in rec.changes:
            moved = [
                (p.ts, entry["new"])
                for p in points
                for entry in p.changes
                if entry.get("key") == change.key and (entry.get("agent") or None) == (change.agent or None)
                and "new" in entry and not (inp.all_projects and p.project and not entry.get("agent"))
            ]
            if moved:
                latest = max(moved, key=lambda item: item[0])[1]
                if _norm(latest) != _norm(change.current):
                    problems.append(
                        f"{_who(rec)} says {change.key}{' of ' + change.agent if change.agent else ''} is "
                        f"{change.current!r}; the Changes page last moved it to {latest!r}"
                    )
    return problems


# -- all seven ----------------------------------------------------------------

CHECKS: dict[int, tuple[str, Callable[[Inputs], list[str]]]] = {
    1: ("tools lines keep what the agent calls", check_tools_lines),
    2: ("a card's agents folder is its fix's", check_agent_folders),
    3: ("totals count only what is on offer", check_totals),
    4: ("a check's figures are its cards'", check_figures),
    5: ("the bundle's counts add up", check_bundle),
    6: ("generated text names only live hints", lambda inp: check_live_hints()),
    7: ("a card and a check give the same advice", check_same_setting),
}


def run_checks(inp: Inputs, numbers=None) -> dict[int, list[str]]:
    """Every check (or those in ``numbers``) over ``inp``, by number. A check
    that cannot run here is reported as one sentence starting "not run"."""
    out: dict[int, list[str]] = {}
    for number, (_name, check) in CHECKS.items():
        if numbers is not None and number not in numbers:
            continue
        try:
            out[number] = check(inp)
        except CheckUnavailable as exc:
            out[number] = [f"not run: {exc}"]
    return out

import re


def test_profile_prompt_lists_changed_unmanaged_keys_with_their_files():
    from claudeglass.fixes import profile_prompt

    rows = [
        {"key": "settings.model", "current_value": "opus", "proposed_value": "sonnet"},
        {"key": "settings.effortLevel", "current_value": "high", "proposed_value": "high"},
        {"key": "settings.promptCacheTtl", "current_value": None, "proposed_value": "1h", "managed": True},
        {"key": "agents.reviewer.effort", "current_value": None, "proposed_value": "low"},
        {"key": "env.MAX_THINKING_TOKENS", "current_value": None, "proposed_value": "8000"},
        {"key": "agents.reviewer.experimental.cacheTtl", "current_value": None, "proposed_value": "1h"},
    ]
    text = profile_prompt("Lean", rows, "repo")
    assert 'In .claude/settings.json, set model to "sonnet" (now: opus).' in text
    assert "effortLevel" not in text and "promptCacheTtl" not in text
    assert 'In .claude/agents/reviewer.md, set effort in the frontmatter to "low" (now: not set).' in text
    assert "environment variable MAX_THINKING_TOKENS" in text
    assert "everyone who works in this project" in text
    assert 'set experimental.cacheTtl in the frontmatter to "1h"' in text


def test_prompt_names_the_model_for_a_per_model_row():
    from claudeglass.fixes import profile_prompt

    rows = [
        {
            "key": "settings.modelSettings.claude-opus-5-5.effortLevel",
            "current_value": "high",
            "proposed_value": "medium",
        },
        {
            "key": "settings.modelSettings.claude-sonnet-4-6[1m].autoCompactWindow",
            "current_value": None,
            "proposed_value": 300000,
        },
        {
            "key": "settings.modelSettings.claude-haiku-4-5.effortLevel",
            "current_value": "low",
            "proposed_value": "low",
        },
    ]
    text = profile_prompt("Lean", rows, "user")
    assert (
        '- In ~/.claude/settings.json, set effortLevel for the model claude-opus-5-5 (under "modelSettings") '
        'to "medium" (now: high).'
    ) in text
    assert (
        '- In ~/.claude/settings.json, set autoCompactWindow for the model claude-sonnet-4-6[1m] '
        '(under "modelSettings") to 300000 (now: not set).'
    ) in text
    # an unchanged row is left out, as for any key
    assert "claude-haiku-4-5" not in text
    # the same rows at a project scope name that scope's file
    assert "In .claude/settings.local.json, set effortLevel for the model claude-opus-5-5" in profile_prompt(
        "Lean", rows, "project-local"
    )


def test_the_flat_effort_level_says_claude_code_keeps_no_max_in_settings():
    from claudeglass.fixes import LEVER_LABELS, SETTING_TEXT

    sentence = "Claude Code doesn't keep max in settings; use /effort max for a session."
    assert SETTING_TEXT["effortLevel"][1].endswith(sentence)
    # the old text is still there, ahead of it
    assert SETTING_TEXT["effortLevel"][1].startswith("Lower effort can miss things on hard problems.")
    # the per-model keys read as their flat keys do, and are labelled for one model
    assert LEVER_LABELS["modelSettings.effortLevel"] == "Effort level for one model"
    assert LEVER_LABELS["modelSettings.autoCompactWindow"] == "Summarise the conversation at (tokens), for one model"
    assert LEVER_LABELS["modelSettings"] == "Per-model settings"
    effort = SETTING_TEXT["modelSettings.effortLevel"]
    assert effort[0] == (
        "How hard Claude thinks, for this model only. It replaces the effort level in the same settings file."
    )
    assert effort[1] == SETTING_TEXT["effortLevel"][1]
    window = SETTING_TEXT["modelSettings.autoCompactWindow"]
    assert window[0] == (
        "When Claude Code summarises the conversation, for this model only. "
        "It replaces the window in the same settings file."
    )
    assert window[1] == SETTING_TEXT["autoCompactWindow"][1]
    assert SETTING_TEXT["modelSettings"][0]


def test_profile_prompt_says_so_when_nothing_changes():
    from claudeglass.fixes import profile_prompt

    text = profile_prompt("Same", [{"key": "settings.model", "current_value": "x", "proposed_value": "x"}], "user")
    assert "nothing to change" in text


def test_profile_overridden_by_names_each_case():
    from claudeglass.fixes import profile_overridden_by

    def by(row_key, scope="user", provenance=None, env_names=(), effort_env_set=False, model_settings=None,
           project_picked=True):
        return profile_overridden_by(
            row_key, scope, provenance or {}, env_names, effort_env_set, model_settings, project_picked
        )

    # A project file that sets the key too, and ranks above the target file.
    shared = "This project's .claude/settings.json sets it too and wins."
    local = "This project's .claude/settings.local.json sets it too and wins."
    assert by("settings.model", provenance={"model": "project_shared"}) == shared
    assert by("settings.model", provenance={"model": "project_local"}) == local
    assert by("settings.model", "repo", {"model": "project_local"}) == local
    # Not above the target file: the same file, a lower one, or the managed layer.
    assert by("settings.model", "repo", {"model": "project_shared"}) is None
    assert by("settings.model", "repo", {"model": "user"}) is None
    assert by("settings.model", "project-local", {"model": "project_shared"}) is None
    assert by("settings.model", "project-local", {"model": "project_local"}) is None
    assert by("settings.model", provenance={"model": "managed"}) is None
    assert by("settings.model", provenance={"model": "user"}) is None
    assert by("settings.model", provenance={"other": "project_shared"}) is None
    # Agent and env rows are not settings keys.
    assert by("agents.builder.model", provenance={"model": "project_shared"}) is None
    assert by("env.MAX_THINKING_TOKENS", provenance={"MAX_THINKING_TOKENS": "project_shared"}) is None

    # The two environment variables.
    window = "CLAUDE_CODE_AUTO_COMPACT_WINDOW is set and wins over this setting."
    assert by("settings.autoCompactWindow", env_names=["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]) == window
    assert by("settings.autoCompactWindow", env_names=["OTHER"]) is None
    assert by("settings.model", env_names=["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]) is None
    effort = "CLAUDE_CODE_EFFORT_LEVEL is set and wins over this setting."
    assert by("settings.effortLevel", effort_env_set=True) == effort
    assert by("settings.model", effort_env_set=True) is None
    assert by("settings.effortLevel") is None

    # A model's own effort level wins for that model.
    assert by("settings.effortLevel", model_settings={"claude-opus-5-5": {"effortLevel": "high"}}) == (
        "claude-opus-5-5 has its own effort level, which wins for it."
    )
    assert by(
        "settings.effortLevel",
        model_settings={"b": {"effortLevel": "low"}, "a": {"effortLevel": "high"}, "c": {"autoCompactWindow": 200000}},
    ) == "a and b have their own effort level, which wins for them."
    assert by(
        "settings.effortLevel",
        model_settings={"a": {"effortLevel": "low"}, "b": {"effortLevel": "high"}, "c": {"effortLevel": "medium"}},
    ) == "a, b and c have their own effort level, which wins for them."
    assert by("settings.effortLevel", model_settings={"c": {"autoCompactWindow": 200000}}) is None
    assert by("settings.model", model_settings={"a": {"effortLevel": "low"}}) is None

    # Nothing set anywhere.
    assert by("settings.effortLevel", provenance={}, env_names=[], effort_env_set=False, model_settings={}) is None
    assert by("settings.effortLevel", model_settings=None) is None
    # The first that applies: a project file's sentence comes before the rest.
    assert by(
        "settings.effortLevel",
        provenance={"effortLevel": "project_local"},
        effort_env_set=True,
        model_settings={"a": {"effortLevel": "low"}},
    ) == local


def test_profile_overridden_by_names_a_project_file_only_when_a_project_is_picked():
    from claudeglass.fixes import profile_overridden_by

    args = ("settings.model", "user", {"model": "project_shared"}, (), False, None)
    assert profile_overridden_by(*args, True) == "This project's .claude/settings.json sets it too and wins."
    # Without a project picked the snapshot is whichever project's took it, so the sentence would name
    # a project the reader did not choose.
    assert profile_overridden_by(*args, False) is None
    # The environment and per-model sentences do not depend on the pick.
    env = ("settings.effortLevel", "user", {"effortLevel": "project_shared"}, ("CLAUDE_CODE_AUTO_COMPACT_WINDOW",), True, {})
    assert profile_overridden_by(*env, False) == "CLAUDE_CODE_EFFORT_LEVEL is set and wins over this setting."
    window = ("settings.autoCompactWindow", "user", {}, ("CLAUDE_CODE_AUTO_COMPACT_WINDOW",), False, None)
    assert profile_overridden_by(*window, False) == "CLAUDE_CODE_AUTO_COMPACT_WINDOW is set and wins over this setting."
    own = ("settings.effortLevel", "user", {}, (), False, {"m": {"effortLevel": "low"}})
    assert profile_overridden_by(*own, False) == "m has its own effort level, which wins for it."


def test_build_fixes_gives_an_actionable_workflow_rule_a_where_trade_off_undo_explainer_and_a_prompt():
    """UX-8 (rest): a recommendation with no ``SettingChange`` (pure
    workflow advice) used to get either a prompt-only fix with an empty
    ``explainer`` (the three ids in the old ``_WORKFLOW_PROMPTS``) or no
    fix at all (every other id) -- neither carried a where/trade-off/undo
    entry. Every id now gets one via ``_WORKFLOW_EXPLAINER``."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="batch-instructions",
        severity="advice",
        category="workflow",
        title="Queued instructions are re-writing the cache prefix",
        action="Batch queued instructions into a single message.",
        lever=None,
    )
    fixes = build_fixes(rec)
    assert len(fixes) == 1
    fix = fixes[0]
    headings = [pair[0] for pair in fix["explainer"]]
    assert headings == ["Where and who it affects", "Trade-off", "How to undo it"]
    assert fix["prompt"]  # a self-contained request Claude can act on
    assert "{" not in fix["prompt"]  # every placeholder was filled in


def test_build_fixes_gives_a_purely_informational_workflow_card_an_explainer_but_no_prompt():
    """cache-read-dominance, data-quality and window-budget propose no
    change at all -- they still get the explainer (stating so plainly),
    but ``prompt`` is empty: there is nothing to ask Claude to do."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="cache-read-dominance",
        severity="info",
        category="workflow",
        title="Cache reads already dominate spend",
        action="There is little further caching upside here.",
        lever=None,
    )
    fixes = build_fixes(rec)
    assert len(fixes) == 1
    assert fixes[0]["prompt"] == ""
    assert fixes[0]["explainer"]


def test_build_fixes_still_gives_nothing_for_an_id_with_no_workflow_entry_at_all():
    """A ``rec.id`` that appears in neither ``_WORKFLOW_EXPLAINER`` nor
    ``_WORKFLOW_PROMPTS`` gets no fix -- unchanged from before UX-8."""
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(id="not-a-real-id", title="x", action="x", lever=None)
    assert build_fixes(rec) == []


def test_render_fix_skips_the_ask_claude_block_for_an_empty_prompt():
    """UX-8's informational workflow cards have an explainer but no
    prompt (see above) -- ``_render_fix``/``_fix_html`` must not print an
    empty "Ask Claude to do it" code block for them."""
    from claudeglass.render.html import _fix_html
    from claudeglass.render.markdown import _render_fix

    fix = {"key": None, "agent": None, "explainer": [["Trade-off", "None."]], "command": None, "prompt": ""}
    md_lines = _render_fix(fix)
    assert not any("Ask Claude to do it" in line for line in md_lines)
    html = _fix_html(fix)
    assert "Ask Claude to do it" not in html


def test_no_workflow_prompt_repeats_its_title():
    """Every ``_WORKFLOW_PROMPTS`` entry used to splice ``{title_lower}``
    into a lead-in that restated the title mid-sentence, so a real title
    (e.g. "Your main session's context is running large") could show up
    twice, sometimes lowercased. Now the title is quoted exactly once, in
    the opening ``_FINDING_OPEN`` line."""
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    title = "A Distinctive Made-Up Finding Title"
    for template_key in fixes_mod._WORKFLOW_PROMPTS:
        rec_id, _, variant = template_key.partition(":")
        rec = Recommendation(
            id=rec_id,
            variant=variant,
            severity="advice",
            category="workflow",
            title=title,
            why="Some unrelated why sentence that doesn't repeat the title.",
            agent_type="my-agent",
            lever=None,
        )
        (fix,) = fixes_mod.build_fixes(rec)
        assert fix["prompt"].lower().count(title.lower()) <= 1, template_key
        # hook-failures' ${CLAUDE_PROJECT_DIR} is a literal brace pair in
        # the finished text (the template escapes it as "${{...}}" for
        # str.format), not a leftover {agent}/{title} placeholder.
        assert "{agent}" not in fix["prompt"], template_key
        assert "{title" not in fix["prompt"], template_key


def test_each_documented_constant_has_its_own_comment_directly_above_it():
    """The ``#:`` block above an assignment describes that assignment:
    ``PROMPT_SCOPE``'s is about the "from now on" suffix, not the closing
    ``_SETTING_TAIL`` carries."""
    import inspect

    from claudeglass import fixes as fixes_mod

    lines = inspect.getsource(fixes_mod).splitlines()

    def comment_above(name):
        at = next(i for i, line in enumerate(lines) if line.startswith(name + " = "))
        block = []
        while at > 0 and lines[at - 1].startswith("#:"):
            at -= 1
            block.insert(0, lines[at])
        return " ".join(block)

    assert "from now on" in comment_above("PROMPT_SCOPE")
    assert "closing every setting-change prompt" not in comment_above("PROMPT_SCOPE")
    assert "closing every setting-change prompt" in comment_above("_SETTING_TAIL")


def test_scope_ids_get_the_prompt_scope_suffix_and_the_scope_note():
    """The seven "from now on" ids get ``PROMPT_SCOPE`` appended to their
    prompt and ``note: "scope"`` on the fix, so the render layer shows
    ``SCOPE_NOTE`` instead of ``RESTART_NOTE``."""
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    scope_ids = (
        "long-tool-waits",
        "notification-invalidation",
        "batch-instructions",
        "long-context-share",
        "spawn-task-prompt",
        "tool-output-carry",
        "wasted-turns",
    )
    for rec_id in scope_ids:
        rec = Recommendation(
            id=rec_id, severity="advice", category="workflow", title="x", agent_type="agent", lever=None
        )
        (fix,) = fixes_mod.build_fixes(rec)
        assert fix["prompt"].rstrip().endswith(fixes_mod.PROMPT_SCOPE), rec_id
        assert fix.get("note") == "scope", rec_id
        assert fixes_mod.fix_note(fix) == fixes_mod.SCOPE_NOTE, rec_id


def test_tools_list_prompts_for_a_new_agent_file_do_not_say_to_keep_its_tools_the_same():
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation, SettingChange

    def prompt(key, value):
        rec = Recommendation(
            id="spawn-tools-list" if key == "tools" else "spawn-claude-md",
            severity="advice",
            category="settings",
            title="x",
            agent_type="statusline-setup",
            changes=[SettingChange(target="agent", key=key, agent="statusline-setup", value=value, new_agent_file=True)],
        )
        (fix,) = fixes_mod.build_fixes(rec)
        return fix["prompt"]

    tools = prompt("tools", ["Grep", "Read"])
    assert "same name" in tools and "keep its tools the same" not in tools
    assert "Claude Code adds StructuredOutput, SubagentHandback and advisor" in tools
    # Any other setting on a new file still carries the original tool list over.
    assert "keep its tools the same" in prompt("omitClaudeMd", True)


def test_the_workflow_script_variant_of_the_tools_list_card_has_its_own_prompt_and_explainer():
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="spawn-tools-list",
        variant="workflow-script",
        severity="advice",
        category="workflow",
        title="Workflow agents are given tools they rarely call",
        action="In each workflow script that starts agents, pass agentType. tools: Grep, Read.",
        agent_type="workflow-subagent",
    )
    (fix,) = fixes_mod.build_fixes(rec)
    assert fix["key"] is None and fix["command"] is None
    assert "agentType" in fix["prompt"]
    assert "tools: Grep, Read" in fix["prompt"]
    assert [heading for heading, _ in fix["explainer"]]
    assert all(text.strip() for _, text in fix["explainer"])
    # The plain id has no entry of its own: the variant is what selects one.
    plain = Recommendation(
        id="spawn-tools-list", severity="advice", category="workflow", title="x", agent_type="workflow-subagent"
    )
    assert fixes_mod.build_fixes(plain) == []


def test_none_note_ids_get_no_note():
    """limit-pressure, discovery-share and pricing-coverage have a prompt
    but propose nothing a restart would pick back up, so their fix gets
    ``note: "none"``; an id with no prompt at all gets the same, computed
    generically rather than listed by hand."""
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    for rec_id in ("limit-pressure", "discovery-share", "pricing-coverage"):
        rec = Recommendation(
            id=rec_id, severity="advice", category="workflow", title="x", agent_type="agent", lever=None
        )
        (fix,) = fixes_mod.build_fixes(rec)
        assert fix.get("note") == "none", rec_id
        assert fixes_mod.fix_note(fix) == "", rec_id

    rec = Recommendation(id="cache-read-dominance", severity="info", category="workflow", title="x", lever=None)
    (fix,) = fixes_mod.build_fixes(rec)
    assert fix["prompt"] == ""
    assert fix.get("note") == "none"
    assert fixes_mod.fix_note(fix) == ""


def test_workflow_fix_prepends_a_why_row_when_why_is_set():
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="batch-instructions",
        severity="advice",
        category="workflow",
        title="Queued instructions are re-writing the cache prefix",
        why="Each message you queue is written to the cache on its own.",
        lever=None,
    )
    (fix,) = build_fixes(rec)
    assert fix["explainer"][0] == ["Why it's suggested", rec.why]


def test_workflow_fix_has_no_why_row_when_why_is_unset():
    from claudeglass.fixes import build_fixes
    from claudeglass.model import Recommendation

    rec = Recommendation(id="batch-instructions", severity="advice", category="workflow", title="x", lever=None)
    (fix,) = build_fixes(rec)
    assert fix["explainer"][0][0] != "Why it's suggested"


def test_render_fix_prints_scope_note_instead_of_restart_note():
    from claudeglass.fixes import RESTART_NOTE, SCOPE_NOTE
    from claudeglass.render.html import _fix_html
    from claudeglass.render.markdown import _render_fix

    fix = {"key": None, "agent": None, "explainer": [], "command": None, "prompt": "Do the thing.", "note": "scope"}
    md_lines = _render_fix(fix)
    assert SCOPE_NOTE in md_lines and RESTART_NOTE not in md_lines
    html = _fix_html(fix)
    assert SCOPE_NOTE in html and RESTART_NOTE not in html


def test_render_fix_prints_restart_note_by_default():
    from claudeglass.fixes import RESTART_NOTE
    from claudeglass.render.html import _fix_html
    from claudeglass.render.markdown import _render_fix

    fix = {"key": None, "agent": None, "explainer": [], "command": None, "prompt": "Do the thing."}
    md_lines = _render_fix(fix)
    assert RESTART_NOTE in md_lines
    html = _fix_html(fix)
    assert RESTART_NOTE in html


def test_render_fix_prints_no_note_when_note_is_none():
    from claudeglass.fixes import RESTART_NOTE, SCOPE_NOTE
    from claudeglass.render.html import _fix_html
    from claudeglass.render.markdown import _render_fix

    fix = {"key": None, "agent": None, "explainer": [], "command": None, "prompt": "Do the thing.", "note": "none"}
    md_lines = _render_fix(fix)
    assert RESTART_NOTE not in md_lines and SCOPE_NOTE not in md_lines
    html = _fix_html(fix)
    assert RESTART_NOTE not in html and SCOPE_NOTE not in html


def test_baseline_bloat_points_at_where_mcp_servers_really_live():
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    rec = Recommendation(id="baseline-bloat", severity="advice", category="settings", title="x", lever=None)
    (fix,) = fixes_mod.build_fixes(rec)
    where = dict(fix["explainer"])["Where and who it affects"]
    assert "~/.claude.json" in where and "settings.json's mcpServers" not in where
    assert "~/.claude.json" in fix["prompt"] and ".mcp.json" in fix["prompt"]


def test_a_workflow_prompt_can_carry_the_cards_own_action():
    """``mcp-unused-server``'s fix differs per server, so its prompt takes
    the card's action (``{action}``) after the finding, which opens it
    once."""
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="mcp-unused-server",
        severity="advice",
        category="workflow",
        title="2 MCP servers you never use are loaded into your sessions",
        why="They were offered in 40 main sessions.",
        action="Notes: run `claude mcp remove notes --scope user`.",
        lever=None,
    )
    (fix,) = fixes_mod.build_fixes(rec)
    opening = fixes_mod._FINDING_OPEN.format(title=rec.title)
    assert fix["prompt"].startswith(f"{opening} {rec.why} ")
    assert fix["prompt"].count(opening) == 1
    assert rec.action in fix["prompt"] and "{action}" not in fix["prompt"]
    assert "never its tokens" in fix["prompt"]


def _agent_model_rec(rec_id: str, variant: str = ""):
    from claudeglass.model import Recommendation

    return Recommendation(
        id=rec_id,
        variant=variant,
        severity="advice",
        category="workflow",
        title="5 workflow agents wrote code on Opus 5.5 with no model set",
        why="4 implementers and 1 fixer in 1 workflow run started with no model, most recently on 1 October.",
        action="Paste the prompt below so Claude sets the model on every agent it starts.",
        agent_type="workflow-subagent",
        lever="model",
    )


def test_each_agent_model_card_gets_one_fix_whose_prompt_quotes_the_finding_and_ends_as_its_kind_does():
    """The three agent-model cards have no setting to change, so each gets
    one workflow fix: a prompt that opens with the finding and has no brace
    left over from ``str.format``. The two "from now on" rules end in
    ``PROMPT_SCOPE``; the one that edits files ends in ``PROMPT_RESTART``."""
    from claudeglass import fixes as fixes_mod

    endings = {
        "agent-model-inherited": fixes_mod.PROMPT_SCOPE,
        "agent-model-asked": fixes_mod.PROMPT_RESTART,
        "agent-decide-apply": fixes_mod.PROMPT_SCOPE,
    }
    for rec_id, ending in endings.items():
        rec = _agent_model_rec(rec_id)
        fixes = fixes_mod.build_fixes(rec)
        assert len(fixes) == 1, rec_id
        prompt = fixes[0]["prompt"]
        assert prompt.startswith(f"{fixes_mod._FINDING_OPEN.format(title=rec.title)} {rec.why} "), rec_id
        assert "{" not in prompt and "}" not in prompt, rec_id
        assert prompt.endswith(ending), rec_id
        assert "_FORCE" not in prompt, rec_id
        assert [pair[0] for pair in fixes[0]["explainer"]] == [
            "Why it's suggested",
            "Where and who it affects",
            "Trade-off",
            "How to undo it",
        ], rec_id
        assert fixes[0]["command"] is None and fixes[0]["key"] is None, rec_id


def test_the_agent_model_notes_follow_the_prompt():
    """A rule to keep gets the scope note, as the other "from now on" ids
    do; the file edit keeps the restart note."""
    from claudeglass import fixes as fixes_mod

    for rec_id in ("agent-model-inherited", "agent-decide-apply"):
        (fix,) = fixes_mod.build_fixes(_agent_model_rec(rec_id))
        assert fix.get("note") == "scope", rec_id
        assert fixes_mod.fix_note(fix) == fixes_mod.SCOPE_NOTE, rec_id
    (fix,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-asked"))
    assert "note" not in fix
    assert fixes_mod.fix_note(fix) == fixes_mod.RESTART_NOTE


def test_the_fixed_asked_card_asks_only_for_a_check_not_a_change():
    from claudeglass import fixes as fixes_mod

    (fix,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-asked", "fixed"))
    assert "Propose sonnet" not in fix["prompt"]
    assert "Change nothing" in fix["prompt"]


def test_the_inherited_prompt_asks_for_the_rule_and_the_fixed_card_keeps_it():
    from claudeglass import fixes as fixes_mod

    (plain,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-inherited"))
    (fixed,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-inherited", "fixed"))
    assert fixed["prompt"] == plain["prompt"] and fixed["explainer"] == plain["explainer"]
    prompt = plain["prompt"]
    assert "From now on, set the model on every subagent and workflow agent you start." in prompt
    assert "Never leave an agent to inherit my session's model, even where a tool's instructions say to omit it." in prompt
    assert "Use Sonnet for agents that write code to a settled spec" in prompt
    assert "Use Opus for agents that decide: integrate, review, verify and judge." in prompt
    assert "Never give one Opus agent both the deciding and the applying." in prompt


def test_the_inherited_explainer_gives_the_call_the_file_and_the_variable():
    from claudeglass import fixes as fixes_mod

    (fix,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-inherited"))
    rows = dict(fix["explainer"])
    where = rows["Where and who it affects"]
    assert where.startswith(fixes_mod._SCOPE_WHERE_TEXT)
    assert "agent(brief, { phase: 'Implement', model: 'sonnet' })" in where
    assert "meta.phases" in where and "only labels the phase" in where
    assert "model: sonnet line in its agent file" in where and "replaces it whole" in where
    tradeoff = rows["Trade-off"]
    assert "CLAUDE_CODE_SUBAGENT_MODEL=sonnet" in tradeoff and "also moves reviewers and judges" in tradeoff
    assert "any model a call or agent file sets still wins" in tradeoff
    assert "never reaches Explore, Plan or forks" in tradeoff
    assert rows["How to undo it"] == fixes_mod._SCOPE_UNDO_TEXT
    assert "_FORCE" not in " ".join(rows.values())


def test_the_asked_and_decide_apply_explainers():
    from claudeglass import fixes as fixes_mod

    (asked,) = fixes_mod.build_fixes(_agent_model_rec("agent-model-asked"))
    rows = dict(asked["explainer"])
    assert "workflow script that names the model" in rows["Where and who it affects"]
    assert "Keep Opus where an agent has to decide as well as write." in rows["Trade-off"]
    assert rows["How to undo it"] == "Set the model back to opus where you changed it."

    (split,) = fixes_mod.build_fixes(_agent_model_rec("agent-decide-apply"))
    rows = dict(split["explainer"])
    assert rows["Where and who it affects"] == fixes_mod._SCOPE_WHERE_TEXT
    assert "the decider's report has to be exact enough" in rows["Trade-off"]
    assert rows["How to undo it"] == fixes_mod._SCOPE_UNDO_TEXT
    assert "have it report the exact changes instead of making them" in split["prompt"]
    assert "Keep the deciding agent on Opus." in split["prompt"]
    assert "started with model set to opus or fable" in asked["prompt"]


def test_plan_rounds_prompt_asks_for_a_critique_before_a_plan_and_names_the_places_to_put_it():
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id="plan-rounds", severity="advice", category="workflow", title="Plans keep being sent back", lever=None,
    )
    (fix,) = fixes_mod.build_fixes(rec)
    assert fixes_mod.CRITIQUE_PLAN_LINE == "Before you show me a plan, critique it for gaps and doubts, then fix them"
    assert fix["prompt"].count(fixes_mod.CRITIQUE_PLAN_LINE[1:]) == 1
    assert "From now on, before you show me a plan, critique it" in fix["prompt"]
    assert "{" not in fix["prompt"]
    assert [pair[0] for pair in fix["explainer"]] == ["Where and who it affects", "Trade-off", "How to undo it"]
    rows = dict(fix["explainer"])
    assert rows["Where and who it affects"].startswith(fixes_mod._SCOPE_WHERE_TEXT)
    assert "plan skill" in rows["Where and who it affects"]
    assert rows["How to undo it"] == fixes_mod._SCOPE_UNDO_TEXT
    # It adds a standing instruction at a scope you pick: the scope note.
    assert fixes_mod.fix_note(fix) == fixes_mod.SCOPE_NOTE


def _bundle_rec(key, changes=(), *, rec_id="model-tier", variant="", scope="user", agent_type=None):
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    rec = Recommendation(
        id=rec_id,
        variant=variant,
        severity="advice",
        category="settings",
        title="Title",
        why="Because.",
        action="Do it.",
        scope=scope,
        agent_type=agent_type,
        changes=list(changes),
        key=key,
    )
    fixes_mod.attach_fixes([rec])
    return rec


def _agent_change(agent="x", key="model", **kw):
    from claudeglass.model import SettingChange

    return SettingChange(target="agent", key=key, agent=agent, value="sonnet", **kw)


def test_project_prompt_groups_changes_under_one_header_per_file():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    a = _bundle_rec("a", [_agent_change("x", "model")])
    b = _bundle_rec("b", [_agent_change("x", "effort")])
    c = _bundle_rec("c", [SettingChange(key="model", value="sonnet")])
    d = _bundle_rec("d", [SettingChange(key="env.MAX_THINKING_TOKENS", value="8000")])
    out = project_prompt([a, b, c, d])
    assert out["prompt"].count("## ~/.claude/agents/x.md") == 1
    assert out["prompt"].count("## ~/.claude/settings.json") == 1
    assert "## ~/.claude/settings.json (env block)" not in out["prompt"]
    assert out["files"] == [{"file": "~/.claude/agents/x.md", "fixes": 2}, {"file": "~/.claude/settings.json", "fixes": 2}]
    assert out["cards"] == 4
    for rec in (a, b, c, d):
        assert fixes_mod._bundle_body(rec.fixes[0]["prompt"])[0] in out["prompt"]


def test_project_prompt_leaves_out_fixed_unconfirmed_managed_and_skipped_cards():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    keep = _bundle_rec("keep", [_agent_change("x")])
    fixed = _bundle_rec("fixed", rec_id="agent-model-asked", variant="fixed")
    unconfirmed = _bundle_rec("unc", [SettingChange(key="disallowedTools", value=["Skill(a)"], unconfirmed=True)])
    managed = _bundle_rec("man", [SettingChange(key="model", value="sonnet", scope="managed")])
    ignored = _bundle_rec("ign", [_agent_change("y")])
    out = project_prompt([keep, fixed, unconfirmed, managed, ignored], {"ign"})
    assert out["cards"] == 1
    assert out["left_out"] == {"fixed": 1, "info": 0, "unconfirmed": 1, "managed": 1}
    assert fixes_mod._bundle_body(keep.fixes[0]["prompt"])[0] in out["prompt"]
    for rec in (fixed, unconfirmed, managed, ignored):
        assert fixes_mod._bundle_body(rec.fixes[0]["prompt"])[0] not in out["prompt"]


def test_project_prompt_states_identical_scope_prompts_once():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import project_prompt

    a = _bundle_rec("agent-model-inherited-a", rec_id="agent-model-inherited", agent_type="a")
    b = _bundle_rec("agent-model-inherited-b", rec_id="agent-model-inherited", agent_type="b")
    assert a.fixes[0]["prompt"] == b.fixes[0]["prompt"]
    out = project_prompt([a, b])
    body = fixes_mod._bundle_body(a.fixes[0]["prompt"])[0]
    assert out["prompt"].count(body) == 1
    assert out["prompt"].startswith(fixes_mod._BUNDLE_OPEN)
    assert "\n\n## CLAUDE.md (Claude asks which)\n\n" in out["prompt"]
    assert out["files"] == []
    assert out["several"] == 1
    assert out["cards"] == 2


def test_project_prompt_leaves_out_info_cards():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import project_prompt

    info = _bundle_rec("info", [_agent_change("w", "omitClaudeMd")], rec_id="spawn-claude-md")
    info.severity = "info"
    out = project_prompt([info])
    assert out["prompt"] == "" and out["cards"] == 0
    assert out["left_out"]["info"] == 1
    assert fixes_mod._bundle_body(info.fixes[0]["prompt"])[0] not in out["prompt"]


def test_project_prompt_states_the_closing_once():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import PROMPT_RESTART, project_prompt
    from claudeglass.model import SettingChange

    recs = [
        _bundle_rec("a", [_agent_change("x", "model")]),
        _bundle_rec("c", [SettingChange(key="model", value="sonnet")]),
        _bundle_rec("agent-model-inherited-a", rec_id="agent-model-inherited", agent_type="a"),
        _bundle_rec("wasted", rec_id="wasted-turns"),
    ]
    out = project_prompt(recs)
    text = out["prompt"]
    assert text.count("Change nothing else") == 0
    assert text.count("Change nothing beyond") == 1
    assert text.count(PROMPT_RESTART) == 0
    assert text.count("remind me once to restart") == 1
    assert text.count("use AskUserQuestion") == 1
    assert len(text) < sum(len(r.fixes[0]["prompt"]) for r in recs)
    assert fixes_mod._bundle_body(recs[0].fixes[0]["prompt"])[0] in text


def test_project_prompt_puts_a_workflow_card_under_the_matching_settings_header():
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    c = _bundle_rec("c", [SettingChange(key="model", value="sonnet")])
    w = _bundle_rec("w", rec_id="compaction-churn")
    out = project_prompt([c, w])
    assert out["prompt"].count("## ~/.claude/settings.json") == 1
    assert "## ~/.claude/settings.json or" not in out["prompt"]
    assert out["files"] == [{"file": "~/.claude/settings.json", "fixes": 2}]


def test_project_prompt_does_not_count_no_file_cards_as_files():
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    limit = _bundle_rec("l", rec_id="limit-pressure")
    c = _bundle_rec("c", [SettingChange(key="model", value="sonnet")])
    out = project_prompt([limit, c])
    assert out["files"] == [{"file": "~/.claude/settings.json", "fixes": 1}]
    assert out["other"] == 1
    assert out["prompt"].index("## No file") > out["prompt"].index("## ~/.claude/settings.json")


def test_project_prompt_counts_only_a_concrete_file_as_a_file():
    """A path, or an "A or B" pair of paths, is a file. A header that names
    a kind of file ("Hook scripts and settings.json"), the CLAUDE.md that
    Claude asks about and a standing rule each span several files or set a
    rule: they are counted apart, by prompt, and are not in ``files``."""
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    setting = _bundle_rec("c", [SettingChange(key="model", value="sonnet")])
    pair = _bundle_rec("pair", rec_id="compaction-churn")
    hooks = _bundle_rec("hooks", rec_id="hook-failures")
    hooks_again = _bundle_rec("hooks2", rec_id="hook-block-resent")
    mcp = _bundle_rec("mcp", rec_id="baseline-bloat")
    shared = _bundle_rec("shared", rec_id="spawn-shared-claude-md")
    rule = _bundle_rec("rule", rec_id="wasted-turns")
    plan = _bundle_rec("plan", rec_id="plan-handoff")
    limit = _bundle_rec("limit", rec_id="limit-pressure")
    out = project_prompt([setting, pair, hooks, hooks_again, mcp, shared, rule, plan, limit])
    assert out["files"] == [
        {"file": "~/.claude/settings.json", "fixes": 2},
        {"file": "~/.claude/CLAUDE.md", "fixes": 1},
    ]
    assert out["several"] == 5
    assert out["other"] == 1
    for header in (
        "## Hook scripts and settings.json",
        "## CLAUDE.md (Claude asks which)",
        "## CLAUDE.md files and agent files",
    ):
        assert header in out["prompt"], header


def test_project_prompt_counts_an_a_or_b_pair_as_a_file():
    from claudeglass.fixes import project_prompt

    pair = _bundle_rec("pair", rec_id="compaction-churn")
    out = project_prompt([pair])
    assert out["files"] == [{"file": "~/.claude/settings.json or .claude/settings.json", "fixes": 1}]
    assert out["several"] == 0


def _header_kind_problems():
    """The ``_FIX_FILE`` ids whose kind is wrong for their header: a header
    is a ``file`` exactly when each of its "A or B" parts is one path (no
    spaces), so a header added without a kind row cannot count as a file. A
    ``rule`` row may sit on a one-path header: its prompt names that path and
    can reach other files too (``run-split`` adds a line to the agent's own
    file)."""
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    problems = []
    for variant_id in fixes_mod._FIX_FILE:
        rec_id, _, variant = variant_id.partition(":")
        rec = Recommendation(id=rec_id, variant=variant, agent_type="x")
        header, kind = fixes_mod.fix_group(rec, None)
        one_path = all(re.fullmatch(r"\S+", part) for part in header.split(" or "))
        if kind == "rule" and one_path:
            continue
        if (kind == "file") != one_path:
            problems.append((variant_id, header, kind))
    return problems


def test_a_header_counts_as_a_file_exactly_when_it_is_one_path_or_an_a_or_b_pair():
    assert _header_kind_problems() == []


def test_a_new_header_without_a_kind_row_is_caught():
    from claudeglass import fixes as fixes_mod

    fixes_mod._FIX_FILE["brand-new-rule"] = "Skill files and settings.json"
    try:
        assert [p[0] for p in _header_kind_problems()] == ["brand-new-rule"]
    finally:
        del fixes_mod._FIX_FILE["brand-new-rule"]


def test_project_prompt_counts_a_partly_included_card():
    from claudeglass import fixes as fixes_mod
    from claudeglass.fixes import project_prompt
    from claudeglass.model import SettingChange

    mix = _bundle_rec(
        "mix", [_agent_change("x"), SettingChange(key="disallowedTools", value=["Skill(a)"], unconfirmed=True)]
    )
    out = project_prompt([mix])
    assert out["cards"] == 1 and out["left_out"]["unconfirmed"] == 1
    skipped = [f for f in mix.fixes if f["key"] == "disallowedTools"]
    assert skipped and fixes_mod._bundle_body(skipped[0]["prompt"])[0] not in out["prompt"]


def test_project_prompt_is_empty_without_open_fixes():
    from claudeglass.fixes import project_prompt

    assert project_prompt([]) == {
        "prompt": "",
        "files": [],
        "several": 0,
        "other": 0,
        "cards": 0,
        "left_out": {"fixed": 0, "info": 0, "unconfirmed": 0, "managed": 0},
    }


def test_every_file_workflow_prompt_resolves_to_a_file():
    from claudeglass import fixes as fixes_mod
    from claudeglass.model import Recommendation

    for variant_id in fixes_mod._WORKFLOW_PROMPTS:
        rec_id, _, variant = variant_id.partition(":")
        if fixes_mod._NOTE_OVERRIDES.get(rec_id) == "scope":
            continue
        rec = Recommendation(id=rec_id, variant=variant, agent_type="x")
        label = fixes_mod.fix_file(rec, None)
        assert label and label != "Other", variant_id


def test_a_run_split_card_is_counted_as_a_standing_rule_not_as_a_file_to_edit():
    """Its prompt adds an instruction to ~/.claude/CLAUDE.md and a line to the
    agent's own file when there is one, so it is not one exact file."""
    from claudeglass.fixes import fix_group, project_prompt

    split = _bundle_rec("run-split:reviewer", rec_id="run-split", agent_type="reviewer")
    assert "agents/reviewer.md" in split.fixes[0]["prompt"]
    assert fix_group(split, None) == ("~/.claude/CLAUDE.md", "rule")
    out = project_prompt([split])
    assert out["files"] == [] and out["several"] == 1 and out["cards"] == 1
    assert "## ~/.claude/CLAUDE.md" in out["prompt"]
    # A card that edits only CLAUDE.md is still a file.
    plan = _bundle_rec("plan", rec_id="plan-handoff")
    assert fix_group(plan, None) == ("~/.claude/CLAUDE.md", "file")


def test_a_one_file_fix_and_a_standing_rule_under_one_header_count_the_same_in_either_order():
    """plan-handoff edits ~/.claude/CLAUDE.md; run-split sets a rule there and may add a line to
    the agent's file. The header is a file to edit, the rule is counted apart, whichever comes first."""
    from claudeglass.fixes import project_prompt

    plan = _bundle_rec("plan", rec_id="plan-handoff")
    split = _bundle_rec("run-split:reviewer", rec_id="run-split", agent_type="reviewer")
    pair = _bundle_rec("pair", rec_id="compaction-churn")
    seen = []
    for order in ([plan, split, pair], [split, plan, pair], [pair, split, plan]):
        out = project_prompt(order)
        assert sorted(out["files"], key=lambda f: f["file"]) == [
            {"file": "~/.claude/CLAUDE.md", "fixes": 1},
            {"file": "~/.claude/settings.json or .claude/settings.json", "fixes": 1},
        ]
        assert out["several"] == 1 and out["other"] == 0 and out["cards"] == 3
        seen.append((sorted(f["file"] for f in out["files"]), out["several"], out["other"]))
    assert seen[0] == seen[1] == seen[2]
    # The prompt still has one section for the shared header, with both fixes in it.
    both = project_prompt([split, plan])["prompt"]
    assert both.count("## ~/.claude/CLAUDE.md") == 1


def test_a_rule_alone_under_a_one_path_header_is_not_a_file_to_edit():
    from claudeglass.fixes import project_prompt

    out = project_prompt([_bundle_rec("run-split:reviewer", rec_id="run-split", agent_type="reviewer")])
    assert out["files"] == [] and out["several"] == 1



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


def test_profile_prompt_says_so_when_nothing_changes():
    from claudeglass.fixes import profile_prompt

    text = profile_prompt("Same", [{"key": "settings.model", "current_value": "x", "proposed_value": "x"}], "user")
    assert "nothing to change" in text


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

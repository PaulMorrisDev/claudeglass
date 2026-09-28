"""Skills review: each listed skill's description (read from the newest
listing, never stored), where it comes from, how often it was listed
and used, and fixes for the unused and the long-winded
(``skills_review``)."""

from __future__ import annotations

import json
from pathlib import Path

from claudeglass import skills_review
from claudeglass.units import Units

from helpers import attachment_line, elasticity_with_slope, write_jsonl

UNITS = Units(billing_mode="api", currency="USD")
SUBSCRIPTION_UNITS = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
PERIOD = "over the last 30 days"


def _usage(name: str, *, listed: int, invoked: int = 0, tokens: int = 40, cost: float = 0.5) -> dict:
    return {
        "name": name,
        "listing_tokens": tokens,
        "listed": {"main": listed, "Explore": 2},
        "listing_cost_usd": cost,
        "invoked": invoked,
        "invoked_by": {"main": invoked} if invoked else {},
        "resent_tokens": 0,
        "resent_cost_usd": 0.0,
        "attributed_turns": 0,
        "attributed_cost_usd": 0.0,
        "last_seen": "2026-09-18T12:00:00Z",
    }


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    config_dir.mkdir(parents=True)
    (claude_root / "skills" / "grill-me").mkdir(parents=True)
    (claude_root / "skills" / "grill-me" / "SKILL.md").write_text("---\nname: grill-me\n---\n", encoding="utf-8")
    project = tmp_path / "repo"
    (project / ".claude" / "workflows").mkdir(parents=True)
    (project / ".claude" / "workflows" / "qa-round.js").write_text("", encoding="utf-8")
    listing = attachment_line(
        "skill_listing",
        content=(
            "- grill-me: Interview the user about a plan.\n"
            "- impeccable:impeccable: Design: polish the UI.\n"
            "- qa-round: Adversarial QA sweep.\n"
            "- dataviz: Charts.\n"
        ),
        skillCount=4,
        names=["grill-me", "impeccable:impeccable", "qa-round", "dataviz"],
    )
    (claude_root / "projects" / "C--repo").mkdir(parents=True)
    write_jsonl(claude_root / "projects" / "C--repo" / "s1.jsonl", [listing])
    return config_dir, project


def _review(tmp_path: Path, rows: list[dict]) -> dict:
    config_dir, project = _setup(tmp_path)
    return skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project])


def test_descriptions_are_read_from_the_newest_listing(tmp_path):
    config_dir, _project = _setup(tmp_path)
    texts = skills_review.descriptions(config_dir.parent)
    assert texts["impeccable:impeccable"] == "Design: polish the UI."
    assert texts["grill-me"] == "Interview the user about a plan."


def test_source_is_worked_out_from_disk(tmp_path):
    data = _review(tmp_path, [])
    sources = {row["name"]: row["source"] for row in data["skills"]}
    assert sources == {
        "grill-me": "user",
        "impeccable:impeccable": "plugin",
        "qa-round": "workflow",
        "dataviz": "built-in",
    }


def test_unused_skills_get_a_hide_fix_with_a_merge_prompt_and_dry_run_command(tmp_path):
    data = _review(
        tmp_path,
        [_usage("grill-me", listed=5), _usage("dataviz", listed=5), _usage("qa-round", listed=5, invoked=2)],
    )
    rows = {row["name"]: row for row in data["skills"]}
    assert rows["grill-me"]["status"] == "unused"
    assert rows["qa-round"]["status"] == "used"
    assert rows["qa-round"]["fixes"] == []
    assert data["unused"] == 2

    hide = rows["dataviz"]["fixes"][0]
    assert hide["command"] == (
        "claudeglass apply --set skillOverrides=dataviz:user-invocable-only --scope user --dry-run"
    )
    assert 'add "dataviz": "user-invocable-only" to skillOverrides, keeping every entry already there' in hide["prompt"]
    headings = [pair[0] for pair in hide["explainer"]]
    assert "Trade-off" in headings and "How to undo it" in headings

    # Your own skill also gets the frontmatter route.
    titles = [fix["title"] for fix in rows["grill-me"]["fixes"]]
    assert "Or turn off automatic use in the skill's own file" in titles
    frontmatter = rows["grill-me"]["fixes"][1]["prompt"]
    assert "disable-model-invocation: true" in frontmatter and "/grill-me" in frontmatter

    [hide_all] = data["fixes"]
    assert hide_all["title"] == "Hide all 2 unused skills from Claude"
    assert "skillOverrides=" in hide_all["command"] and "grill-me:user-invocable-only" in hide_all["command"]


def test_a_review_of_one_project_hides_skills_in_that_project_only(tmp_path):
    """Its sessions are all it saw, and a skill Claude never used there
    may be used in another project."""
    config_dir, project = _setup(tmp_path)
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"skillOverrides": {"qa-round": "off"}}), encoding="utf-8"
    )
    rows = [_usage("grill-me", listed=5), _usage("dataviz", listed=5), _usage("qa-round", listed=5)]
    data = skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project], only=(project,))
    by_name = {row["name"]: row for row in data["skills"]}
    assert by_name["qa-round"]["hidden"] == "skillOverrides sets it to off"
    hide = by_name["dataviz"]["fixes"][0]
    assert "--set skillOverrides=dataviz:user-invocable-only --scope project-local --project-dir ." in hide["command"]
    assert ".claude/settings.local.json" in hide["prompt"]
    # Your own skill's file is read in every project: no frontmatter route.
    assert [fix["title"] for fix in by_name["grill-me"]["fixes"]] == ["Hide it from Claude, keep it in your / menu"]
    [hide_all] = data["fixes"]
    assert "--scope project-local" in hide_all["command"]
    assert "hidden in that project only" in skills_review.render_markdown(data)

    # Several projects, or one whose folder isn't known: nothing to hide.
    data = skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project], only=())
    assert data["fixes"] == [] and all(row["fixes"] == [] for row in data["skills"])
    assert data["limited_text"].startswith("Only some projects were read")
    # Every project: your user settings, as before.
    data = skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project])
    assert data["limited_text"] == "" and "--scope user" in data["fixes"][0]["command"]


def test_a_project_slug_is_found_by_the_folder_its_sessions_ran_in(tmp_path):
    config_dir, project = _setup(tmp_path)
    write_jsonl(config_dir.parent / "projects" / "C--repo" / "s2.jsonl", [{"type": "user", "cwd": str(project)}])
    assert skills_review.project_folders_for(config_dir.parent, ["C--repo", "C--gone"]) == (project,)


def test_long_description_of_a_used_skill_gets_a_shorten_prompt(tmp_path):
    data = _review(tmp_path, [_usage("grill-me", listed=5, invoked=1, tokens=250)])
    row = next(row for row in data["skills"] if row["name"] == "grill-me")
    [fix] = row["fixes"]
    assert fix["title"] == "Shorten its description"
    assert "SKILL.md" in fix["prompt"] and fix["command"] is None


def test_nothing_is_stored_and_markdown_renders(tmp_path):
    data = _review(tmp_path, [_usage("dataviz", listed=5), _usage("grill-me", listed=4)])
    assert json.dumps(data)  # JSON-ready
    markdown = skills_review.render_markdown(data)
    assert markdown.startswith("# Skills review")
    assert "## Hide all 2 unused skills from Claude" in markdown
    assert "## dataviz (Built into Claude Code)" in markdown
    assert "never used" in markdown


def test_markdown_listing_cost_line_has_no_bare_dollar_or_doubled_about_under_a_subscription(tmp_path):
    """UX-2 / finding F3: the opening "Skill listings take about N
    tokens..." line's cost clause must route through Units, never a bare
    "$", and must not double "about" (a subscription's own share text
    already opens with "about X% of your weekly usage limit")."""
    config_dir, project = _setup(tmp_path)
    data = skills_review.review(
        config_dir, {"skills": [_usage("dataviz", listed=5), _usage("grill-me", listed=4)]},
        SUBSCRIPTION_UNITS, PERIOD, projects=[project],
    )
    markdown = skills_review.render_markdown(data)
    opening = markdown.splitlines()[2]  # "# Skills review", "", "Skill listings take about..."
    assert opening.startswith("Skill listings take about")
    assert "$" not in opening
    assert "about about" not in opening.lower()


def test_skills_the_settings_already_hide_get_no_fix(tmp_path):
    """Their listings in the window predate the change, and suggesting
    user-invocable-only for a skill set to off would show it again."""
    config_dir, project = _setup(tmp_path)
    (config_dir.parent / "settings.json").write_text(
        json.dumps(
            {
                "skillOverrides": {"grill-me": "off"},
                "enabledPlugins": {"impeccable@impeccable": False, "other@inline": True},
            }
        ),
        encoding="utf-8",
    )
    rows = [_usage("grill-me", listed=5), _usage("impeccable:impeccable", listed=5), _usage("dataviz", listed=5)]
    data = skills_review.review(config_dir, {"skills": rows}, UNITS, PERIOD, projects=[project])
    by_name = {row["name"]: row for row in data["skills"]}

    assert by_name["grill-me"]["status"] == "hidden" and by_name["grill-me"]["fixes"] == []
    assert by_name["grill-me"]["hidden"] == "skillOverrides sets it to off"
    assert by_name["impeccable:impeccable"]["hidden"] == "its plugin is turned off"
    assert by_name["dataviz"]["status"] == "unused"
    assert data["unused"] == 1 and data["fixes"] == []
    assert "Already hidden: skillOverrides sets it to off." in skills_review.render_markdown(data)


def test_a_skill_a_claude_code_tool_loads_is_never_offered_for_hiding(tmp_path):
    """The Workflow tool tells Claude to load workflow-authoring; hidden,
    that instruction points at a skill Claude can't load. It gets
    name-only instead, which keeps the name the tool points at."""
    rows = [
        _usage("workflow-authoring", listed=5, tokens=60, cost=0.6),
        _usage("dataviz", listed=5),
        _usage("grill-me", listed=5),
    ]
    data = _review(tmp_path, rows)
    by_name = {row["name"]: row for row in data["skills"]}

    row = by_name["workflow-authoring"]
    assert (row["status"], row["needed_by"]) == ("needed by a tool", "Workflow")
    [fix] = row["fixes"]
    assert fix["title"] == "List it by name only"
    assert fix["command"] == (
        "claudeglass apply --set skillOverrides=workflow-authoring:name-only --scope user --dry-run"
    )
    # "- workflow-authoring" is about 5 tokens of the 60 kept.
    assert "0.55 USD" in dict(fix["explainer"])["Expected effect"]

    assert data["unused"] == 2 and data["needed_by_a_tool"] == 1
    [hide_all] = data["fixes"]
    assert "workflow-authoring" not in hide_all["command"]
    assert "Left out: workflow-authoring. Claude Code's own Workflow tool tells Claude to load it" in hide_all["prompt"]
    assert "Not offered for hiding: Claude Code's Workflow tool tells Claude to load it." in (
        skills_review.render_markdown(data)
    )


def test_only_the_built_in_skill_of_that_name_counts_as_tool_loaded(tmp_path):
    config_dir, project = _setup(tmp_path)
    (config_dir.parent / "skills" / "workshop").mkdir()
    (config_dir.parent / "skills" / "workshop" / "SKILL.md").write_text("---\nname: workshop\n---\n", encoding="utf-8")
    data = skills_review.review(config_dir, {"skills": [_usage("workshop", listed=5)]}, UNITS, PERIOD, projects=[project])
    row = next(row for row in data["skills"] if row["name"] == "workshop")
    assert (row["source"], row["status"], row["needed_by"]) == ("user", "unused", "")


def test_a_skill_no_longer_listed_and_gone_from_disk_is_not_offered_for_hiding(tmp_path):
    """A workflow deleted weeks ago still has listings in the window;
    with no file left and no listing since, hiding it saves nothing."""
    old = {**_usage("scorecard-delta", listed=5), "last_seen": "2026-08-01T09:00:00Z"}
    dataviz = {**_usage("dataviz", listed=5), "last_seen": "2026-08-01T09:00:00Z"}
    rows = [old, _usage("grill-me", listed=5), _usage("qa-round", listed=5), dataviz]
    data = _review(tmp_path, rows)
    by_name = {row["name"]: row for row in data["skills"]}

    row = by_name["scorecard-delta"]
    assert (row["source"], row["source_label"], row["status"], row["fixes"]) == (
        "removed", "Removed", "no longer listed", []
    )
    # A skill with a file on disk stays, however long since it was listed.
    assert by_name["qa-round"]["status"] == "unused"
    assert by_name["dataviz"]["status"] == "no longer listed"
    assert data["unused"] == 2
    [hide_all] = data["fixes"]
    assert "scorecard-delta" not in hide_all["command"]
    assert "No longer listed, and no file for it is left on disk" in skills_review.render_markdown(data)


def test_a_built_in_skill_listed_recently_is_still_built_in(tmp_path):
    rows = [_usage("dataviz", listed=5), {**_usage("grill-me", listed=5), "last_seen": "2026-09-30T12:00:00Z"}]
    data = _review(tmp_path, rows)
    row = next(row for row in data["skills"] if row["name"] == "dataviz")
    assert (row["source"], row["status"]) == ("built-in", "unused")


def test_a_plugin_enabled_under_any_marketplace_is_not_hidden():
    settings = {"enabledPlugins": {"impeccable@a": False, "impeccable@b": True}}
    assert skills_review.hidden_by("impeccable:impeccable", "plugin", settings) == ""
    assert skills_review.hidden_by("grill-me", "user", {"skillOverrides": {"grill-me": "on"}}) == ""

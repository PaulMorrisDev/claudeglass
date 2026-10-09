"""CLAUDE.md review: files found on disk, joined to their usage by salted
path hash, with sections, agent-only sections, duplicates, stale
references and fix prompts (``claude_md_review``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import claude_md_review as cmr
from claudeglass import parse
from claudeglass.units import Units

from helpers import elasticity_with_slope

SALT = b"r" * 32
UNITS = Units(billing_mode="api", currency="USD")
PERIOD = "over the last 30 days"

SHARED = (
    "Always run the full test suite before you commit anything, and never skip the pre-commit hooks "
    "without asking first."
)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    config_dir.mkdir(parents=True)
    (claude_root / "CLAUDE.md").write_text(f"# Global\n\n{SHARED}\n", encoding="utf-8")
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("", encoding="utf-8")
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "agents" / "db-migrator.md").write_text("---\nname: db-migrator\n---\n", encoding="utf-8")
    (project / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    big = "Rule line that matters for every task in this repo.\n" * 40
    (project / "CLAUDE.md").write_text(
        "# Project\n\n"
        f"{SHARED}\n\n"
        "## Layout\n\nCode lives in `src/app.py` and the old entry point `src/legacy/main.py`.\n"
        "Run `npm run test` or `npm run e2e`.\n\n"
        "## db-migrator notes\n\nOnly the migration agent needs this.\n\n"
        f"## Conventions\n\n{big}",
        encoding="utf-8",
    )
    return config_dir, project


def _review(tmp_path: Path, context_files: dict | None = None):
    config_dir, project = _setup(tmp_path)
    return cmr.build_review(config_dir, context_files or {}, salt=SALT, projects=[project]), project


def test_finds_user_and_project_files_with_sections(tmp_path):
    review, project = _review(tmp_path)
    levels = {item.level: item for item in review.files}
    assert {"User", "Project"} <= set(levels)
    headings = [s.heading for s in levels["Project"].sections]
    assert headings[:4] == ["Project", "Layout", "db-migrator notes", "Conventions"]
    assert levels["Project"].id == parse.path_hash(str(project / "CLAUDE.md"), SALT)


def test_a_review_limited_to_some_projects_reads_only_their_files_and_yours(tmp_path):
    config_dir, project = _setup(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "CLAUDE.md").write_text("# Other\n\nOther notes.\n", encoding="utf-8")

    def projects(selected):
        return {item.project for item in cmr.build_review(config_dir, {}, salt=SALT, projects=selected).files}

    assert projects([project, other]) == {"", "repo", "other"}
    assert projects([project]) == {"", "repo"}
    # A project whose folder isn't known: yours only. Every project gets those.
    assert projects([]) == {""}


def test_flags_agent_sections_duplicates_and_stale_references(tmp_path):
    review, _project = _review(tmp_path)
    project_file = next(item for item in review.files if item.level == "Project")
    agent_section = next(s for s in project_file.sections if s.heading == "db-migrator notes")
    assert agent_section.agents == ["db-migrator"]
    assert any("Always run the full test suite" in d["excerpt"] for d in project_file.duplicates)
    references = {item["reference"] for item in project_file.stale}
    assert "src/legacy/main.py" in references
    assert "src/app.py" not in references
    assert "npm run e2e" in references and "npm run test" not in references


def test_a_one_word_agent_name_counts_only_where_it_names_the_agent():
    """An agent called claude (or Explore, or Plan) is not named by
    "Claude Code", ".claude/" or "plan the change"."""
    names = ["claude", "Plan", "db-migrator"]
    prose = (
        "Claude Code reads `.claude/rules/` first. Claude should plan the change, then Claude runs the tests. "
        "See .claude/agents/ and the Claude Agent SDK docs."
    )
    assert cmr._agents_in("RevIXO — Claude Code Context", prose, names) == []
    assert cmr._agents_in("Notes", "Use the Plan agent first. The Plan agent reads only.", names) == ["Plan"]
    assert cmr._agents_in("Notes", "Spawn `claude` for this; `claude` has every tool.", names) == ["claude"]
    assert cmr._agents_in("Notes", 'subagent_type: "Plan" and subagent_type="Plan"', names) == ["Plan"]
    assert cmr._agents_in("db-migrator notes", "", names) == ["db-migrator"]


def test_usage_joins_by_hash_and_fixes_carry_the_undo_and_diff_step(tmp_path):
    config_dir, project = _setup(tmp_path)
    file_hash = parse.path_hash(str(project / "CLAUDE.md"), SALT)
    usage = {
        "transcripts": {"main": 4, "Explore": 2},
        "files": [
            {
                "hash": file_hash,
                "type": "Project",
                "scoped": False,
                "tokens": 700,
                "sends": 6,
                "reach": {"main": 4, "db-migrator": 2},
                "cost_usd": 1.5,
                "cost_by_reach": {"main": 1.0, "db-migrator": 0.5},
                "last_seen": "2026-09-18T12:00:00Z",
            }
        ],
    }
    review = cmr.build_review(config_dir, usage, salt=SALT, projects=[project])
    project_file = next(item for item in review.files if item.level == "Project")
    detail = cmr.file_detail(project_file, UNITS, PERIOD)
    assert detail["seen"] and detail["sends"] == 6
    assert "4 main sessions" in detail["reach_text"]
    assert detail["cost_text"]
    titles = [fix["title"] for fix in detail["fixes"]]
    assert "Move agent-only sections into agent files" in titles
    assert "Fix references to things that no longer exist" in titles
    for fix in detail["fixes"]:
        headings = [pair[0] for pair in fix["explainer"]]
        assert "Trade-off" in headings and "How to undo it" in headings
        assert "show me the diff" in fix["prompt"].lower()
    markdown = cmr.render_markdown(review, UNITS, PERIOD)
    assert "# CLAUDE.md review" in markdown and "db-migrator" in markdown


def test_trim_this_file_fix_has_no_bare_dollar_or_doubled_about_under_a_subscription():
    """UX-2 / finding F3: "Trim this file"'s effect clause
    (f"About {...} if you halve it.") must route through Units and
    ``.phrase(prefix="About ")``, never a bare "$" and never "About
    about ..." (a subscription's own share text already opens with
    "about")."""
    subscription = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    review = cmr.FileReview(
        id="trim-me",
        path=Path("CLAUDE.md"),
        level="Project",
        project="repo",
        chars=cmr.TRIM_TOKENS * 4 * 2,  # well over the trim threshold
        scoped=False,
        sections=[],
        imports=[],
        usage={"cost_usd": 3.0, "sends": {"main": 4}},
    )
    fixes = cmr.build_fixes(review, subscription, PERIOD)
    trim = next(f for f in fixes if f["title"] == "Trim this file")
    effect = next(value for label, value in trim["explainer"] if label == "Expected effect")
    assert "$" not in effect
    assert "about about" not in effect.lower()


def test_sections_ignore_headings_inside_code_fences():
    text = "# One\n\ntext\n\n```bash\n# not a heading\n```\n\n## Two\n\nmore\n"
    assert [s.heading for s in cmr.sections(text)] == ["One", "Two"]


def test_worktrees_fold_into_their_main_project(tmp_path):
    main = tmp_path / "repo"
    worktree = main / ".claude" / "worktrees" / "wt1"
    worktree.mkdir(parents=True)
    folders, worktrees = cmr.split_worktrees([main, worktree])
    assert folders == [main]
    assert list(worktrees.values()) == [[worktree]]


def test_project_index_matches_subfolder_paths_and_prefixes(tmp_path):
    (tmp_path / "src" / "App" / "Shared").mkdir(parents=True)
    (tmp_path / "src" / "App" / "Shared" / "Foo.cs").write_text("", encoding="utf-8")
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "067_add_users.sql").write_text("", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    index = cmr._ProjectIndex(tmp_path)

    assert index.has("Shared/Foo.cs")
    assert index.has(r"App\Shared\foo.cs")
    assert index.has("./src/App")
    assert index.has("sql/067")
    assert index.has("sql/0xx")  # a placeholder, not a path
    assert not index.has("hared/Foo.cs")  # part of a folder name is not a folder
    assert not index.has("Shared/Bar.cs")
    assert not index.has("node_modules/pkg")
    assert not index.has("Foo.cs\nsql")


def test_project_index_is_kept_across_reviews_for_a_while(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cmr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cmr, "_INDEXES", {})
    config_dir, project = _setup(tmp_path)
    cmr.build_review(config_dir, {}, salt=SALT, projects=[project])
    first = cmr._project_index(project)

    clock[0] += cmr._INDEX_TTL_S - 1
    cmr.build_review(config_dir, {}, salt=SALT, projects=[project])
    assert cmr._project_index(project) is first

    clock[0] += 1
    assert cmr._project_index(project) is not first


# -- project files: naming the hashes transcripts keep, and what to do about one -----------------


@pytest.fixture
def fresh_names():
    """``local_names`` keeps its answer for a while: start and end empty."""
    cmr._NAMES.clear()
    yield
    cmr._NAMES.clear()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """A project holding docs, source, and the folders a walk must skip."""
    config_dir, project = _setup(tmp_path)
    (project / "docs").mkdir()
    (project / "docs" / "context.md").write_text("Context for the agents.\n", encoding="utf-8")
    (project / "docs" / "notes.txt").write_text("Notes.\n", encoding="utf-8")
    for skipped in (".git", "node_modules/pkg", "dist", "build", "__pycache__"):
        (project / skipped).mkdir(parents=True)
        (project / skipped / "inside.md").write_text("Never named.\n", encoding="utf-8")
    return config_dir, project


def _hash(path: Path, salt: bytes = SALT) -> str:
    return parse.path_hash(str(path), salt)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("context.md", "md"),
        ("NOTES.MD", "md"),
        ("guide.mdx", "md"),
        ("readme.txt", "txt"),
        ("spec.rst", "txt"),
        ("data.json", "json"),
        ("log.jsonl", "json"),
        ("ci.yaml", "config"),
        ("pyproject.toml", "config"),
        ("app.py", "code"),
        ("Program.cs", "code"),
        ("archive.xyz", "other"),
        ("Makefile", "other"),
        ("", "other"),
    ],
)
def test_ext_class_is_a_word_from_a_closed_set(name, expected):
    assert cmr.ext_class(name) == expected
    assert cmr.ext_class(name) in cmr.EXT_CLASSES


def test_local_names_gives_each_project_file_its_path_from_the_project_folder(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    found = cmr.local_names(config_dir, projects=[project], salt=SALT)

    assert found.names[_hash(project / "docs" / "context.md")] == {
        "name": "docs/context.md",
        "ext": "md",
        "project": "repo",
    }
    assert found.names[_hash(project / "docs" / "notes.txt")]["ext"] == "txt"
    assert found.names[_hash(project / "src" / "app.py")] == {"name": "src/app.py", "ext": "code", "project": "repo"}
    assert found.truncated is False


def test_local_names_matches_the_hash_transcripts_keep_for_the_same_path(tmp_path, fresh_names):
    """A transcript hashes the path with a forward-slash, any-case spelling:
    the file on disk must come out as the same hash."""
    config_dir, project = _project(tmp_path)
    found = cmr.local_names(config_dir, projects=[project], salt=SALT)
    spelled = str(project / "docs" / "context.md").replace("\\", "/").upper()
    assert parse.path_hash(spelled, SALT) in found.names


def test_local_names_skips_git_node_modules_and_build_folders(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    found = cmr.local_names(config_dir, projects=[project], salt=SALT)

    assert not [item for item in found.names.values() if item["name"].endswith("inside.md")]
    for skipped in (".git", "node_modules/pkg", "dist", "build", "__pycache__"):
        assert _hash(project / skipped / "inside.md") not in found.names
    assert cmr._WALK_SKIP >= {".git", "node_modules", "dist", "build"}


def test_local_names_stops_at_the_cap_and_says_so(tmp_path, fresh_names):
    config_dir, project = _setup(tmp_path)
    many = project / "many"
    many.mkdir()
    for index in range(30):
        (many / f"file{index:02d}.md").write_text("x", encoding="utf-8")

    capped = cmr.local_names(config_dir, projects=[project], salt=SALT, max_files=10)
    in_project = [item for item in capped.names.values() if item["project"] == "repo"]
    assert capped.truncated is True
    assert len(in_project) == 10

    whole = cmr.local_names(config_dir, projects=[project], salt=SALT)
    assert whole.truncated is False
    assert len([item for item in whole.names.values() if item["project"] == "repo"]) > 30


def test_the_walk_takes_shallow_files_first_so_a_cap_keeps_the_ones_that_matter(tmp_path, fresh_names):
    config_dir, project = _setup(tmp_path)
    deep = project / "a" / "b" / "c"
    deep.mkdir(parents=True)
    for index in range(20):
        (deep / f"deep{index:02d}.md").write_text("x", encoding="utf-8")
    capped = cmr.local_names(config_dir, projects=[project], salt=SALT, max_files=8)
    assert _hash(project / "CLAUDE.md") in capped.names
    assert _hash(project / "package.json") in capped.names


def test_the_walk_is_bounded_in_depth_and_in_folders(tmp_path, monkeypatch):
    deep = tmp_path
    for index in range(cmr._NAMES_MAX_DEPTH + 3):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)
    (deep / "too-deep.md").write_text("x", encoding="utf-8")
    (tmp_path / "top.md").write_text("x", encoding="utf-8")
    files, cut = cmr._walk_files(tmp_path, 100)
    assert [path.name for path in files] == ["top.md"] and cut is False

    monkeypatch.setattr(cmr, "_NAMES_MAX_DIRS", 2)
    for name in ("x", "y", "z"):
        (tmp_path / name).mkdir()
    _files, cut = cmr._walk_files(tmp_path, 100)
    assert cut is True


def test_a_worktrees_copy_of_a_file_gets_the_same_name_as_the_main_projects(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    worktree = project / ".claude" / "worktrees" / "feat"
    (worktree / "docs").mkdir(parents=True)
    (worktree / "docs" / "context.md").write_text("A copy.\n", encoding="utf-8")

    found = cmr.local_names(config_dir, projects=[project, worktree], salt=SALT)
    assert found.names[_hash(worktree / "docs" / "context.md")]["name"] == "docs/context.md"
    assert found.names[_hash(project / "docs" / "context.md")]["name"] == "docs/context.md"
    # The worktree is not walked as a project of its own.
    assert not [item for item in found.names.values() if item["project"] == "feat"]


def test_only_the_wanted_hashes_are_kept(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    wanted = _hash(project / "docs" / "context.md")
    found = cmr.local_names(config_dir, projects=[project], salt=SALT, wanted=[wanted])
    assert list(found.names) == [wanted]
    everything = cmr.local_names(config_dir, projects=[project], salt=SALT)
    assert len(everything.names) > 5


def test_the_user_files_plans_and_rules_are_named_outside_a_project(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    claude_root = config_dir.parent
    (claude_root / "plans").mkdir()
    (claude_root / "plans" / "big-plan.md").write_text("A plan.\n", encoding="utf-8")
    (claude_root / "rules").mkdir()
    (claude_root / "rules" / "style.md").write_text("A rule.\n", encoding="utf-8")

    found = cmr.local_names(config_dir, projects=[project], salt=SALT)
    for path in (claude_root / "plans" / "big-plan.md", claude_root / "rules" / "style.md", claude_root / "CLAUDE.md"):
        item = found.names[_hash(path)]
        assert item["project"] == "" and item["ext"] == "md"


def test_the_project_folders_come_from_the_transcripts_when_none_are_given(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    folder = config_dir.parent / "projects" / "repo-slug"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text(json.dumps({"type": "user", "cwd": str(project)}) + "\n", encoding="utf-8")

    found = cmr.local_names(config_dir, salt=SALT)
    assert found.names[_hash(project / "docs" / "context.md")]["name"] == "docs/context.md"
    # No folders known: only yours.
    empty = tmp_path / "elsewhere" / ".claude"
    (empty / "claudeglass").mkdir(parents=True)
    assert cmr.local_names(empty / "claudeglass", salt=SALT).names == {}


def test_the_salt_is_the_one_the_transcripts_were_hashed_with(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    salt = parse.load_or_create_salt(config_dir)
    found = cmr.local_names(config_dir, projects=[project])
    assert _hash(project / "docs" / "context.md", salt) in found.names
    other = cmr.local_names(config_dir, projects=[project], salt=b"z" * 32)
    assert not set(other.names) & set(found.names)


def test_local_names_is_kept_for_a_while_and_depends_on_what_is_asked_for(tmp_path, monkeypatch, fresh_names):
    clock = [1000.0]
    monkeypatch.setattr(cmr.time, "monotonic", lambda: clock[0])
    config_dir, project = _project(tmp_path)
    first = cmr.local_names(config_dir, projects=[project], salt=SALT)
    (project / "docs" / "later.md").write_text("New.\n", encoding="utf-8")

    clock[0] += cmr._NAMES_TTL_S - 1
    assert cmr.local_names(config_dir, projects=[project], salt=SALT) is first
    assert cmr.local_names(config_dir, projects=[project], salt=SALT, wanted=["x"]) is not first

    clock[0] += 1
    again = cmr.local_names(config_dir, projects=[project], salt=SALT)
    assert again is not first and _hash(project / "docs" / "later.md") in again.names


def test_a_file_a_claude_md_imports_is_found_named_and_tied_to_its_importer(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    guide = project / "docs" / "guide.md"
    guide.write_text("G" * 400, encoding="utf-8")
    (project / "CLAUDE.md").write_text("# Project\n\nSee @docs/guide.md for the guide.\n", encoding="utf-8")

    # Only the importer is wanted, yet the import is named: it is how it was found.
    found = cmr.local_names(config_dir, projects=[project], salt=SALT, wanted=[_hash(project / "CLAUDE.md")])
    child = _hash(guide)
    assert found.imports == {child}
    assert found.inlined[child] == {"parent": _hash(project / "CLAUDE.md"), "tokens": 100}
    assert found.names[child] == {"name": "docs/guide.md", "ext": "md", "project": "repo"}
    assert found.as_dict() == {"names": found.names, "imports": [child], "inlined": found.inlined}


def test_an_import_that_is_not_on_disk_is_not_a_file(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    (project / "CLAUDE.md").write_text("See @docs/missing.md.\n", encoding="utf-8")
    found = cmr.local_names(config_dir, projects=[project], salt=SALT)
    assert found.imports == set() and found.inlined == {}


def test_project_file_rows_name_the_files_the_report_has_and_leave_the_rest_unnamed(tmp_path, fresh_names):
    config_dir, project = _project(tmp_path)
    salt = parse.load_or_create_salt(config_dir)
    known = _hash(project / "docs" / "context.md", salt)
    data = {
        "transcripts": {"main": 5, "Explore": 5},
        "window_days": 30.0,
        "newest": "2026-09-30",
        "files": [],
        "reads": [
            {
                "hash": hash_,
                "source": "read",
                "tokens": 6000,
                "reach": {"Explore": 5},
                "cost_usd": cost,
                "weekly": {},
                "last_seen": "2026-09-30T10:00:00.000Z",
            }
            for hash_, cost in ((known, 3.0), ("0123456789abcdef", 1.0))
        ],
    }
    rows, local = cmr.project_file_rows(config_dir, data, projects=[project])

    by_hash = {row["hash"]: row for row in rows}
    assert (by_hash[known]["name"], by_hash[known]["ext"], by_hash[known]["project"]) == ("docs/context.md", "md", "repo")
    assert by_hash["0123456789abcdef"]["name"] == ""
    # Only what the report asked for was kept from the walk.
    assert set(local.names) == {known}
    assert rows[0]["hash"] == known


# -- what to do about a project file -------------------------------------------------------------


def _file_row(**over) -> dict:
    row = {
        "hash": "aaaa",
        "source": "read",
        "name": "docs/context.md",
        "project": "repo",
        "ext": "md",
        "tokens": 9000,
        "change_pct": 60.0,
        "cost_month_usd": 12.0,
        "reach": [
            {"reach": "main", "runs": 4, "share": 0.4, "standing": True},
            {"reach": "Explore", "runs": 6, "share": 0.6, "standing": True},
            {"reach": "Plan", "runs": 5, "share": 0.5, "standing": True},
            {"reach": "Review", "runs": 1, "share": 0.1, "standing": False},
        ],
        **over,
    }
    return row


def test_a_project_file_gets_five_prompts_to_copy_and_edits_nothing():
    fixes = cmr.project_file_fixes(_file_row(), UNITS)
    assert [fix["title"] for fix in fixes] == [
        "Trim what is stale",
        "Split it by who needs it",
        "Move rule-like parts into path-scoped rules",
        "Move reference material into a skill",
        "Put the essential lines in the agent definition and drop the read",
    ]
    for fix in fixes:
        assert fix["command"] is None and fix["key"] is None and fix["agent"] is None
        assert fix["prompt"].endswith(cmr._PROMPT_TAIL)
        assert "docs/context.md (in repo) is about 9,000 tokens and is read by Explore, Plan." in fix["prompt"]
        assert "It grew 60% in about 30 days." in fix["prompt"]
        assert [heading for heading, _text in fix["explainer"]] == [
            "What this changes",
            "Now and after",
            "Where and who it affects",
            "Expected effect",
            "Trade-off",
            "How to undo it",
        ]


def test_the_explainer_says_who_reads_the_file_and_what_cutting_it_saves():
    [fix, *_rest] = cmr.project_file_fixes(_file_row(), UNITS)
    sections = dict(fix["explainer"])
    assert sections["Where and who it affects"] == "docs/context.md (in repo): read by Explore, Plan."
    assert "Now: about 9,000 tokens, read by 2 agent types." in sections["Now and after"]
    # 12 a month over 9,000 tokens is about 1.33 for every 1,000 cut.
    assert sections["Expected effect"] == (
        "It costs 12.00 USD a month now. Every 1,000 tokens cut saves about 1.33 USD a month."
    )


def test_a_file_that_is_also_loaded_for_the_agents_says_so():
    [fix, *_rest] = cmr.project_file_fixes(_file_row(source="import"), UNITS)
    assert "and loaded for them by Claude Code" in dict(fix["explainer"])["Where and who it affects"]


def test_a_file_no_agent_type_reads_by_habit_has_four_prompts_and_none_to_drop_a_read():
    row = _file_row(reach=[{"reach": "main", "runs": 4, "share": 0.4, "standing": True}], change_pct=None)
    fixes = cmr.project_file_fixes(row, UNITS)
    assert len(fixes) == 4
    assert not [fix for fix in fixes if "drop the read" in fix["title"]]
    assert "read by your agents" in dict(fixes[0]["explainer"])["Where and who it affects"]
    assert "It grew" not in fixes[0]["prompt"]


def test_an_imported_file_gets_no_prompt_to_drop_a_read():
    fixes = cmr.project_file_fixes(_file_row(source="import"), UNITS)
    assert len(fixes) == 4
    assert not [fix for fix in fixes if "drop the read" in fix["title"]]


def test_the_project_file_saving_says_about_once_under_a_subscription():
    subscription = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    effect = dict(cmr.project_file_fixes(_file_row(), subscription)[0]["explainer"])["Expected effect"]
    assert "about about" not in effect.lower()


def test_a_file_without_a_cost_does_not_promise_a_saving():
    [fix, *_rest] = cmr.project_file_fixes(_file_row(cost_month_usd=0.0), UNITS)
    effect = dict(fix["explainer"])["Expected effect"]
    assert "Every 1,000 tokens" not in effect and "USD" not in effect


def test_a_file_this_machine_cannot_find_has_no_prompts_to_copy():
    assert cmr.project_file_fixes(_file_row(name="", project="", ext=""), UNITS) == []


def test_a_file_outside_any_project_is_named_by_its_path_alone():
    [fix, *_rest] = cmr.project_file_fixes(_file_row(name="~/.claude/plans/big.md", project=""), UNITS)
    assert fix["prompt"].startswith("~/.claude/plans/big.md is about 9,000 tokens")
    assert "(in " not in fix["prompt"].split(".", 1)[0]

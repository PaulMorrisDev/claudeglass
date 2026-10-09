"""Tests for ``footprint.py`` and ``init``'s connect question, ``changes``
and ``uninstall`` commands: what claudeglass installs, and taking
every part of it back out, always showing the change and backing up
settings.json first."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

import pytest

from claudeglass import capture_catalogue as cat, cli, footprint, hook_health, installer, setup_flow
from claudeglass.profiles import apply as apply_mod

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
HOOK_CMD = f'"{sys.executable}" "C:/x/claudeglass/hooks/snapshot-config.py"'
STATUS_CMD = f'"{sys.executable}" -m claudeglass.statusline'


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """Claude Code's folder is ``$CLAUDE_CONFIG_DIR`` (``discovery.claude_root``),
    never worked out from the data folder: point it at the ``claude``
    folder these tests build ``settings.json`` in."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _claude(tmp_path, settings=None):
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    config_dir.mkdir(parents=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir


def test_plan_connect_adds_hook_and_statusline_and_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=STATUS_CMD)
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert len(plan.changes) == 2
    assert "adds no tokens" in plan.changes[0]
    assert "terminal only" in plan.changes[1]
    after = json.loads(plan.new_text)
    entry = after["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry == {"type": "command", "command": HOOK_CMD, "async": True}
    assert after["statusLine"]["command"] == STATUS_CMD
    assert after["model"] == "opus"
    assert "+  \"statusLine\"" in plan.diff


def test_plan_connect_never_replaces_your_own_statusline(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-line"}})
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=STATUS_CMD)
    assert json.loads(plan.new_text)["statusLine"]["command"] == "my-line"
    assert len(plan.changes) == 1


def test_connect_backs_up_then_writes(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    plan = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=None)
    backup = hook_health.connect(plan, now=NOW)
    assert backup.name == "settings.json.bak-20260923T120000Z"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"model": "opus"}
    again = hook_health.plan_connect(config_dir, hook_command=HOOK_CMD, statusline_command=None)
    assert again.new_text is None and again.changes == []


def test_uninstall_plan_removes_only_this_tools_entries(tmp_path):
    settings = {
        "model": "opus",
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo mine"}, {"type": "command", "command": HOOK_CMD}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
        },
        "statusLine": {"type": "command", "command": STATUS_CMD},
    }
    config_dir = _claude(tmp_path, settings)
    plan = footprint.plan_uninstall(config_dir)
    assert len(plan.settings_changes) == 2
    after = json.loads(plan.new_settings_text)
    assert after["hooks"]["SessionStart"][0]["hooks"] == [{"type": "command", "command": "echo mine"}]
    assert after["hooks"]["Stop"] == settings["hooks"]["Stop"]
    assert "statusLine" not in after
    backup = footprint.remove_settings_entries(plan, now=NOW)
    assert json.loads(backup.read_text(encoding="utf-8")) == settings


def test_uninstall_drops_empty_hook_groups(tmp_path):
    config_dir = _claude(tmp_path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}})
    after = json.loads(footprint.plan_uninstall(config_dir).new_settings_text)
    assert "hooks" not in after


def test_uninstall_leaves_someone_elses_statusline(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-line"}})
    assert footprint.plan_uninstall(config_dir).new_settings_text is None


def _fake_backup(config_dir, ts, *, reverted=False):
    folder = config_dir / "backups" / ts
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps({"ts": ts, "profile_id": "one-off", "scope": "user", "entries": []}), encoding="utf-8"
    )
    if reverted:
        (folder / apply_mod.REVERTED_FILENAME).write_text(json.dumps({"reverted_at": "2026-09-23T00:00:00Z"}))


def test_inventory_lists_every_part_with_an_undo(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    _fake_backup(config_dir, "20260920T000000Z")
    _fake_backup(config_dir, "20260921T000000Z", reverted=True)
    items = {item.key: item for item in footprint.inventory(config_dir, service_registered=None)}
    assert items["snapshot_hook"].status == "not installed"
    assert items["statusline"].status == "installed"
    assert items["service"].status == "unknown"
    assert items["apply:20260920T000000Z"].status == "in place"
    assert items["apply:20260920T000000Z"].undo == "claudeglass apply --revert 20260920T000000Z"
    assert items["apply:20260920T000000Z"].title.startswith("Applied one-off change")
    assert items["apply:20260921T000000Z"].status == "undone"
    assert items["data"].status == "installed"
    for item in items.values():
        assert item.token_cost and item.what_it_does and item.undo


def test_list_backups_reads_the_reverted_marker(tmp_path):
    config_dir = _claude(tmp_path)
    _fake_backup(config_dir, "20260921T000000Z", reverted=True)
    assert apply_mod.list_backups(config_dir)[0].reverted_at == "2026-09-23T00:00:00Z"


def _run(argv, monkeypatch, capsys, stdin=""):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_changes_command_prints_undo_commands(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["changes", "--config-dir", str(config_dir)], monkeypatch, capsys)
    assert rc == 0
    assert "Statusline: installed" in out
    assert "Tokens: None." in out
    assert "claudeglass uninstall" in out


def test_uninstall_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    _fake_backup(config_dir, "20260920T000000Z")
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    rc, out = _run(
        ["uninstall", "--config-dir", str(config_dir), "--dry-run", "--revert-changes", "--delete-data"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert "Dry run: settings.json left unchanged." in out
    assert "Would undo 20260920T000000Z" in out
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert config_dir.is_dir()


def test_uninstall_keeps_data_while_changes_are_in_place(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"model": "opus"})
    _fake_backup(config_dir, "20260920T000000Z")
    rc, out = _run(["uninstall", "--config-dir", str(config_dir), "--yes", "--delete-data"], monkeypatch, capsys)
    assert rc == 1
    assert "Not deleted" in out
    assert config_dir.is_dir()


def test_uninstall_refuses_delete_data_while_hook_entries_remain(tmp_path, monkeypatch, capsys):
    # ROB-P7: declining step 1 (or it failing) leaves settings.json
    # running a hook from the folder --delete-data would remove; Claude
    # Code would then call a script that no longer exists and fail
    # silently on every session.
    config_dir = _claude(tmp_path, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": HOOK_CMD}]}]}})
    rc, out = _run(["uninstall", "--config-dir", str(config_dir), "--delete-data"], monkeypatch, capsys, stdin="n\n")
    assert rc == 1
    assert "Left unchanged." in out  # step 1 declined
    assert "Not deleted" in out and "hook" in out.lower()
    assert config_dir.is_dir()
    assert "SessionStart" in json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))["hooks"]


def test_uninstall_yes_removes_entries_and_data(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"model": "opus", "statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["uninstall", "--config-dir", str(config_dir), "--yes", "--delete-data"], monkeypatch, capsys)
    assert rc == 0, out
    assert json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8")) == {"model": "opus"}
    assert not config_dir.exists()
    assert "pip uninstall claudeglass" in out


def test_uninstall_asks_and_a_no_changes_nothing(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}})
    rc, out = _run(["uninstall", "--config-dir", str(config_dir)], monkeypatch, capsys, stdin="n\n")
    assert "Left unchanged." in out
    assert "statusLine" in json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))


def _init(config_dir, *argv, **tools):
    """``init --non-interactive --connect --no-service``, with any of
    :class:`setup_flow.Tools` replaced; returns ``(rc, output)``."""
    import dataclasses

    args = cli._make_parser().parse_args(
        ["init", "--config-dir", str(config_dir), "--non-interactive", "--connect", "--no-service", *argv]
    )
    options, real = cli._setup_flow_inputs(args)
    out = io.StringIO()
    rc = setup_flow.run(options, dataclasses.replace(real, **tools), stdin=io.StringIO(""), stdout=out, now=NOW)
    return rc, out.getvalue()


def test_init_dry_run_shows_the_connect_change_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    settings = config_dir.parent / "settings.json"
    before = settings.read_text(encoding="utf-8")
    rc, out = _init(config_dir, "--dry-run")
    assert rc == 0
    assert settings.read_text(encoding="utf-8") == before
    assert "Dry run" in out and "SessionStart" in out
    assert list(config_dir.iterdir()) == []


def test_init_refuses_an_unsafe_hook_command(tmp_path):
    # ROB-P9: a hook command that can't be built (a quote/$/backtick/UNC
    # path in the Python or script path) must never reach settings.json
    # as a broken "command": null entry.
    config_dir = _claude(tmp_path, {"model": "opus"})
    settings = config_dir.parent / "settings.json"
    before = settings.read_text(encoding="utf-8")
    rc, out = _init(config_dir, hook_command=None)
    assert rc == 0
    assert settings.read_text(encoding="utf-8") == before
    assert "Connect to Claude Code: not possible, because this Python's path" in out


# --------------------------------------------------------------------
# One rule for where settings.json is: --claude-root, else
# $CLAUDE_CONFIG_DIR, else ~/.claude -- never next to --config-dir.
# --------------------------------------------------------------------


def _elsewhere(tmp_path):
    """A data folder away from Claude Code's folder, with a decoy
    settings.json beside it that nothing may read or write."""
    data = tmp_path / "elsewhere" / "tl-data"
    data.mkdir(parents=True)
    decoy = data.parent / "settings.json"
    decoy.write_text('{"decoy": true}\n', encoding="utf-8")
    return data, decoy


def test_init_connect_with_config_dir_elsewhere_writes_claude_settings(tmp_path):
    claude_settings = _claude(tmp_path, {"model": "opus"}).parent / "settings.json"
    data, decoy = _elsewhere(tmp_path)
    rc, out = _init(data)
    assert rc == 0, out

    assert decoy.read_text(encoding="utf-8") == '{"decoy": true}\n'
    written = json.loads(claude_settings.read_text(encoding="utf-8"))
    hook_cmd = written["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # The hook and the statusline write where the CLI reads.
    assert hook_cmd.endswith(f'--config-dir "{data.resolve()}"')
    assert str(data.resolve() / "hooks" / "snapshot-config.py") in hook_cmd
    assert written["statusLine"]["command"].endswith(f'--config-dir "{data.resolve()}"')
    # The dashboard's own check finds it in the same place.
    assert hook_health.check(data).command == hook_cmd


def test_default_config_dir_adds_no_config_dir_flag(tmp_path):
    config_dir = _claude(tmp_path)
    assert cli._config_dir_args(config_dir) == ""
    assert cli._config_dir_args(tmp_path / "other") == f' --config-dir "{(tmp_path / "other").resolve()}"'


def test_claude_root_flag_wins_over_the_environment(tmp_path):
    other = tmp_path / "other-claude"
    other.mkdir()
    (other / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": STATUS_CMD}}))
    data, decoy = _elsewhere(tmp_path)
    assert hook_health.settings_path(other) == other / "settings.json"
    plan = footprint.plan_uninstall(data, claude_root=other)
    assert plan.settings_path == other / "settings.json"
    assert plan.new_settings_text is not None


def test_uninstall_and_changes_use_claude_settings_not_config_dir_parent(tmp_path, monkeypatch, capsys):
    claude_settings = _claude(tmp_path, {"statusLine": {"type": "command", "command": STATUS_CMD}}).parent / "settings.json"
    data, decoy = _elsewhere(tmp_path)
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)

    assert cli.main(["changes", "--config-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "Statusline: installed" in out

    assert cli.main(["uninstall", "--config-dir", str(data), "--yes"]) == 0
    assert "statusLine" not in json.loads(claude_settings.read_text(encoding="utf-8"))
    assert decoy.read_text(encoding="utf-8") == '{"decoy": true}\n'


def test_uninstall_claude_root_flag(tmp_path, monkeypatch):
    _claude(tmp_path)
    other = tmp_path / "other-claude"
    other.mkdir()
    (other / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": STATUS_CMD}}))
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    data, _decoy = _elsewhere(tmp_path)
    assert cli.main(["uninstall", "--config-dir", str(data), "--claude-root", str(other), "--yes"]) == 0
    assert "statusLine" not in json.loads((other / "settings.json").read_text(encoding="utf-8"))


# -- every hook that can return output runs in the foreground ------------------


def _registered(tmp_path, ids) -> list[tuple[str, dict]]:
    """``(event, entry)`` for every hook entry settings.json would hold
    for the capture metrics in ``ids``."""
    config_dir = tmp_path / "claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    plan = hook_health.plan_capture(hook_health.capture_specs(ids), cli._capture_hook_commands(config_dir))
    after = json.loads(plan.new_text)
    return [
        (event, entry) for event, groups in after["hooks"].items() for group in groups for entry in group["hooks"]
    ]


_EVERYTHING = (*cat.LEVEL_METRIC_IDS, *cat.FEEDBACK_IDS, *cat.COACHING_IDS, cat.HAIKU_TAGGER_HOOK)
_ID_SETS = {
    **{level: (*cat.level_metrics(level), "coaching_notes") for level in cat.LEVELS[1:]},
    "everything": _EVERYTHING,
    "coaching alone": ("coaching_notes",),
    "haiku tagger": (*cat.level_metrics("deep"), cat.HAIKU_TAGGER_HOOK),
}


@pytest.mark.parametrize("name", list(_ID_SETS))
def test_every_hook_entry_that_can_return_output_is_registered_in_the_foreground(tmp_path, name):
    # An async hook's additionalContext and systemMessage only arrive on the
    # next turn: a tip written for this message would reach Claude, and the
    # user, a whole reply late.
    ids = _ID_SETS[name]
    specs = cat.hook_specs(ids)
    assert not [spec for spec in specs if spec[1] in cat.OUTPUT_EVENTS and spec[3]], specs
    for event, entry in _registered(tmp_path, ids):
        if event in cat.OUTPUT_EVENTS:
            assert "async" not in entry, (event, entry)
        else:
            # What runs in the background is only ever a free signal.
            assert not entry.get("async") or event in cat.SIGNAL_EVENTS, (event, entry)


def test_the_output_events_are_all_registered_by_some_metric_set_and_none_is_a_signal(tmp_path):
    seen = {event for name in _ID_SETS for event, _entry in _registered(tmp_path, _ID_SETS[name])}
    assert {"SessionStart", "UserPromptSubmit", "PostToolUse"} <= seen
    assert not set(cat.OUTPUT_EVENTS) & set(cat.SIGNAL_EVENTS)
    # The signals really are background hooks, so the check above can fail.
    async_events = {
        event for name in _ID_SETS for event, entry in _registered(tmp_path, _ID_SETS[name])
        if entry.get("async")
    }
    assert async_events and async_events <= set(cat.SIGNAL_EVENTS)


@pytest.mark.parametrize("name", list(_ID_SETS))
def test_no_hook_entry_waits_on_a_shell_or_mcp_tool(tmp_path, name):
    # Claude Code waits for a PostToolUse hook after every call that matches,
    # and the shell and MCP tools are most of a session's calls: the replay of
    # real sessions found a size note after them wrong too often to pay for it.
    config_dir = tmp_path / "claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    plan = hook_health.plan_capture(hook_health.capture_specs(_ID_SETS[name]), cli._capture_hook_commands(config_dir))
    groups = json.loads(plan.new_text)["hooks"].get("PostToolUse", [])
    for group in groups:
        tools = group["matcher"].split("|")
        assert tools and set(tools) <= set(cat.COACHING_TOOLS), tools
        assert not [tool for tool in tools if tool in ("Bash", "PowerShell") or tool.startswith("mcp__")], tools


def test_coaching_notes_write_the_matcher_with_the_agent_and_workflow_tools_the_other_sets_leave_out(tmp_path):
    config_dir = tmp_path / "claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)

    def written(ids) -> str:
        plan = hook_health.plan_capture(hook_health.capture_specs(ids), cli._capture_hook_commands(config_dir))
        (group,) = json.loads(plan.new_text)["hooks"]["PostToolUse"]
        return group["matcher"]

    assert written(("coaching_notes",)) == "Read|WebFetch|WebSearch|ExitPlanMode|Agent|Workflow"
    assert written((*cat.level_metrics("deep"), "coaching_notes")) == written(("coaching_notes",))
    # Without coaching notes nothing waits on an agent's report or a launch message.
    assert written(cat.level_metrics("deep")) == "Read|WebFetch|WebSearch"


def test_the_capture_hooks_item_says_the_agent_and_workflow_calls_wait_for_coaching_notes_only(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    assert "With coaching notes on, it also waits after an approved plan and an agent or workflow call." in item.what_it_does
    assert (
        "It waits after a finished subagent while coaching notes are on or Claude Haiku judges agent runs."
        in item.what_it_does
    )
    assert "never after a shell command" in item.what_it_does


def test_the_launcher_its_module_and_the_word_list_are_all_one_part_of_the_footprint(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    hook_health.connect(
        hook_health.plan_capture(hook_health.capture_specs(cat.level_metrics("essentials")), cli._capture_hook_commands(config_dir)),
        now=NOW,
    )
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    assert "launcher" in item.what_it_does and "data folder" in item.what_it_does
    assert "never after a shell command" in item.what_it_does
    # Removing the settings entries and then the data folder leaves nothing behind,
    # the launcher's cached bytecode included.
    plan = footprint.plan_uninstall(config_dir)
    assert plan.settings_changes and all("capture-hook.py" in line for line in plan.settings_changes)
    footprint.remove_settings_entries(plan, now=NOW)
    hooks_dir = config_dir / "hooks"
    (hooks_dir / "__pycache__").mkdir()
    (hooks_dir / "__pycache__" / "capture_hook.cpython-311.pyc").write_bytes(b"pyc")
    assert {p.name for p in hooks_dir.iterdir()} >= {cat.HOOK_SCRIPT, cat.HOOK_MODULE, cat.CATALOGUE_FILE, "__pycache__"}
    assert footprint.delete_data(config_dir) == []
    assert not config_dir.exists()


def test_the_capture_hooks_show_while_only_a_feedback_item_runs_them(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\nfeedback = ["feedback_skill"]\n', encoding="utf-8")
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    assert item.status == "not installed" and "/cg-feedback" in item.what_it_does


# -- Phase 7: what a change costs, and what capture's start note says --------

_CACHE_COST = (
    "None by itself. Each session writes its own part of the prompt cache, and a change to the tool set "
    "rewrites the shared part once."
)


def test_an_applied_change_costs_nothing_by_itself_and_says_why_in_cache_terms(tmp_path):
    config_dir = _claude(tmp_path, {})
    _fake_backup(config_dir, "20260920T000000Z")
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=None)}["apply:20260920T000000Z"]
    assert item.token_cost == _CACHE_COST
    # The old line said every session builds its cache from scratch, and that a change then saves or costs more.
    assert "from scratch" not in item.token_cost and "estimate" not in item.token_cost


def test_the_settings_expectation_says_the_same_about_the_cache():
    (text,) = [text for title, text in footprint.EXPECTATIONS if title == "A change takes effect in new sessions"]
    assert "Each session writes its own part of the prompt cache" in text
    assert "a change to the tool set rewrites the shared part once" in text
    assert "from scratch" not in text and "costs nothing extra" not in text
    assert "(/model) does rebuild that session's cache once" in text


def _expect_text(**capture_args) -> str:
    from claudeglass.config import CaptureConfig

    (first, *_) = footprint.expectations(CaptureConfig(**capture_args))
    assert first[0] == "It uses a few of your Claude tokens while capture is on"
    return first[1]


def test_the_expectation_for_capture_is_the_warning_for_what_is_on():
    from claudeglass import capture_view
    from claudeglass.config import CaptureConfig

    config = CaptureConfig(level="standard")
    text = _expect_text(level="standard")
    assert text.startswith("Metrics capture is on (Standard). Claude reads a short note at the start of a session")
    assert capture_view.warning_text(config.active_metrics(), config.tagger).removeprefix(
        "Metrics capture uses your tokens. "
    ) in text
    # Claude reads no note when a subagent starts, and the line no longer says it does.
    assert "subagent starts" not in text and "or subagent" not in text
    assert "subagents and workflow agents are asked for nothing" in text
    assert text.endswith("shows how many. Turn it off with 'claudeglass capture off'.")


def test_the_expectation_for_capture_follows_the_tagger():
    text = _expect_text(level="essentials", tagger="haiku")
    assert "Claude Haiku writes the tags in the background" in text
    assert "Claude reads" not in text and "one-line tag you will see" not in text


def test_the_expectation_for_capture_names_the_tool_note_at_deep_only():
    assert "after a large read, search or web result" in _expect_text(level="deep")
    assert "after a large read" not in _expect_text(level="essentials")


def test_the_capture_hooks_item_says_what_the_start_note_and_the_agents_get(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    hook_health.connect(
        hook_health.plan_capture(hook_health.capture_specs(cat.level_metrics("essentials")), cli._capture_hook_commands(config_dir)),
        now=NOW,
    )
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    text = item.what_it_does
    assert "adds a short note when a session starts (and after /clear or a compaction)" in text
    assert "adds no start note" in text and "Claude Haiku writing the tags" in text
    assert "Subagents and the agents a workflow starts are asked for nothing" in text
    assert "Claude Haiku judges each agent run in the background" in text
    assert "session or subagent starts" not in text

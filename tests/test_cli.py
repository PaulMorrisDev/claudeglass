"""CLI wiring tests (WP10c): the argparse skeleton (``--version``, the
default-subcommand insertion rule) plus real end-to-end tests for every
wired subcommand, against synthetic projects under ``tmp_path`` built
with ``tests/helpers``.

``serve`` (S1-api) and ``init``/``baseline`` (v0.3) are no longer stubs
-- every subcommand in ``cli.SUBCOMMANDS`` is wired for real now.
``tests/test_service_api.py``/``tests/test_service_egress.py`` exercise
``serve`` directly; ``tests/test_onboarding.py``/``tests/test_
baseline.py`` exercise ``onboarding.py``/``baseline.py`` directly, so
this file's own ``init``/``baseline`` tests below are thin CLI-wiring
smoke tests, not full coverage. The "unimplemented subcommand exits 2"
subprocess smoke test now uses ``scrub-fixture`` with none of its
required flags -- a real, always-available bad-input case -- since
there is no longer an actually-unimplemented subcommand to name.

Every test passes ``--projects-root``/``--project`` explicitly rather
than relying on the autouse ``CLAUDE_CONFIG_DIR``/``HOME`` isolation
fixture's fake home directory, so a test's fixtures live wherever
``tmp_path`` puts them regardless of what that fixture points at --
except the cache/snapshot/log-usage tests, which need a real
``--config-dir`` and use ``tmp_path`` for that too.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import pytest

from claudeglass import __version__, baseline as baseline_mod, cli, discovery
from claudeglass.snapshots import snapshot_project_key

from helpers import assert_privacy, turn_line, write_jsonl

_GENERATED_AT_RE = re.compile(r"- Generated at:.*")


def _strip_generated_at(text: str) -> str:
    """Blank out the one line that legitimately differs between two
    otherwise-identical report runs (``ReportMeta.generated_at`` is
    ``datetime.now()`` at render time), so a byte-identical-output
    assertion isn't flaky across a wall-clock second boundary.
    """
    return _GENERATED_AT_RE.sub("- Generated at: STRIPPED", text)


def _write_project(root: Path, slug: str, n_turns: int = 3, age_seconds: int | None = None) -> Path:
    """Write one synthetic session under ``<root>/<slug>/session-1.jsonl``.

    ``age_seconds``, when given, backdates the file's mtime by that many
    seconds -- needed to get the digest cache to actually treat the file
    as cacheable (``cache.py``: a file younger than 60 seconds is always
    a live-session cache miss).
    """
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "session-1.jsonl"
    write_jsonl(
        path,
        [
            turn_line(input_tokens=100 + i, output_tokens=20 + i, cache_read_input_tokens=30)
            for i in range(n_turns)
        ],
    )
    if age_seconds is not None:
        old = path.stat().st_mtime - age_seconds
        os.utime(path, (old, old))
    return project_dir


# -- version / default-subcommand skeleton -----------------------------------


def test_version_exits_zero_and_prints_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_no_argv_inserts_default_subcommand():
    assert cli._insert_default_subcommand([]) == ["report"]


def test_unrecognised_leading_token_inserts_default_subcommand():
    # "--verbose" is a global flag, not a subcommand, so "report" is
    # inserted ahead of it.
    assert cli._insert_default_subcommand(["--verbose"]) == ["report", "--verbose"]


def test_known_subcommand_is_left_alone():
    argv = ["sessions", "--days", "7"]
    assert cli._insert_default_subcommand(argv) == argv


def test_default_subcommand_constant_is_report():
    assert cli.DEFAULT_SUBCOMMAND == "report"


# -- --group-by choices (Fix R6) ---------------------------------------------


def test_group_by_choices_match_classify_group_keys():
    import argparse

    from claudeglass import classify

    parser = cli._make_parser()
    subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    report_parser = subparsers_action.choices["report"]
    group_by_action = next(a for a in report_parser._actions if a.dest == "group_by")
    assert tuple(group_by_action.choices) == tuple(sorted(classify._GROUP_KEYS))


def test_group_by_invalid_choice_exits_2_with_one_line_message(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--group-by", "profile"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    # argparse's own usage banner precedes the error line -- just check
    # the actual error line is the expected single one-line message,
    # not that the whole stderr output is short.
    error_lines = [line for line in err.splitlines() if "invalid choice" in line]
    assert len(error_lines) == 1
    assert "profile" in error_lines[0]


def test_group_by_accepts_entrypoint(tmp_path, capsys):
    # "entrypoint" is a real classify._GROUP_KEYS member that the old
    # hand-maintained choices tuple omitted entirely.
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--group-by", "entrypoint", "--json"]
    )
    assert exit_code == 0
    capsys.readouterr()


def test_init_and_baseline_are_no_longer_marked_planned(capsys):
    # v0.3: init/baseline used to read as "(planned) v0.3 milestone" in
    # the top-level --help listing (fix R25) -- confirm that marker is
    # gone now that both are wired up for real.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for command in ("init", "baseline"):
        lines = [line for line in out.splitlines() if command in line]
        assert lines, f"{command} not found in --help output"
        assert not any("(planned)" in line for line in lines)


def test_init_writes_config_and_runs_an_initial_baseline(tmp_path, monkeypatch, capsys):
    # The project directory is nested under an explicit "home-<name>"
    # segment, so a user name in the project's path is under our control
    # on every platform: init's output must never name it.
    projects_root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    real_project_path = tmp_path / "home-reallife-username" / "work" / "my-proj"
    real_project_path.mkdir(parents=True)
    slug = discovery.slug_for(str(real_project_path))
    _write_project(projects_root, slug)

    monkeypatch.chdir(real_project_path)
    exit_code = cli.main(
        [
            "init",
            "--non-interactive",
            "--no-install",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(projects_root),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert (config_dir / "config.toml").is_file()
    # projects/<slug>.toml is written only by init --advanced.
    assert not (config_dir / "projects").exists()
    assert "Reading this project's history for a first baseline... " in out
    # Fix N3: the absolute config_dir path, and the user name in the
    # project's path, never appear in stdout.
    assert "reallife-username" not in out
    assert str(config_dir) not in out


# --------------------------------------------------------------------
# apply (fix B3): cli._resolve_claude_root, exercised only at this
# level -- tests/test_profiles_apply.py always passes claude_root=
# explicitly to plan_apply, so it can never catch a bug in how cli.py
# itself resolves that value. The old ``home = config_dir.parent``
# computation happened to be right only when --config-dir took its own
# untouched default; these tests deliberately point --config-dir
# somewhere unrelated (as a user legitimately can) while the autouse
# CLAUDE_CONFIG_DIR fixture (tests/conftest.py) supplies the real
# Claude root, so a regression back to the old computation fails them.
# --------------------------------------------------------------------


def _write_profile_toml(path: Path, *, settings: dict | None = None, agents: dict | None = None) -> None:
    lines = [f'id = "{path.stem}"']
    if settings:
        lines.append("[settings]")
        for key, value in settings.items():
            lines.append(f'{key} = "{value}"')
    if agents:
        for agent_name, agent_settings in agents.items():
            lines.append(f"[agents.{agent_name}]")
            for key, value in agent_settings.items():
                lines.append(f'{key} = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cmd_apply_user_scope_resolves_claude_root_independently_of_config_dir(tmp_path):
    claude_root = Path(os.environ["CLAUDE_CONFIG_DIR"])
    config_dir = tmp_path / "somewhere" / "else" / "claudeglass"
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, settings={"effortLevel": "high"})

    exit_code = cli.main(["apply", str(profile_path), "--config-dir", str(config_dir), "--yes"])
    assert exit_code == 0

    settings_path = claude_root / "settings.json"
    assert settings_path.is_file()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {"effortLevel": "high"}
    # The bug's own symptom (fix B3): a nested .claude/.claude/ never
    # gets created under the real Claude root.
    assert not (claude_root / ".claude").exists()


def test_cmd_apply_user_scope_agent_patch_finds_claude_root_agents_file(tmp_path):
    claude_root = Path(os.environ["CLAUDE_CONFIG_DIR"])
    config_dir = tmp_path / "somewhere" / "else" / "claudeglass"
    agents_dir = claude_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: opus\n---\n\nBody.\n", encoding="utf-8")
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, agents={"reviewer": {"model": "sonnet"}})

    exit_code = cli.main(["apply", str(profile_path), "--config-dir", str(config_dir), "--yes"])
    assert exit_code == 0
    assert "model: sonnet" in (agents_dir / "reviewer.md").read_text(encoding="utf-8")


def test_cmd_apply_claude_root_flag_overrides_default(tmp_path):
    explicit_root = tmp_path / "explicit-claude-root"
    config_dir = tmp_path / "claudeglass"
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, settings={"effortLevel": "high"})

    exit_code = cli.main(
        ["apply", str(profile_path), "--config-dir", str(config_dir), "--claude-root", str(explicit_root), "--yes"]
    )
    assert exit_code == 0
    assert (explicit_root / "settings.json").is_file()
    # Never touches the env-derived default root when --claude-root is given.
    assert not (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").exists()


def test_cmd_apply_project_scope_uses_that_projects_own_snapshot_not_the_newest_overall(tmp_path, capsys):
    """COV-04: apply used to resolve `latest_snapshot` as `snaps[-1]` --
    whichever project snapshotted most recently, anywhere -- even for a
    project-scoped apply. A provenance-based warning built from the
    wrong project's snapshot would be meaningless. Two projects here:
    project-b's snapshot is the newer one overall but carries no
    provenance conflict; project-a's own (older) snapshot does. Only
    picking project-a's own snapshot surfaces the warning.
    """
    config_dir = tmp_path / "tl"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    (project_a / ".claude").mkdir(parents=True)
    (project_b / ".claude").mkdir(parents=True)

    snapshots_dir = config_dir / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "20260101T000000Z.json").write_text(
        json.dumps(
            {
                "ts": "20260101T000000Z",
                "schema_version": 2,
                "project_slug": snapshot_project_key(discovery.slug_for(str(project_a))),
                "effective": {"model": "haiku"},
                "effective_provenance": {"model": "project_local"},
            }
        ),
        encoding="utf-8",
    )
    (snapshots_dir / "20260201T000000Z.json").write_text(
        json.dumps(
            {
                "ts": "20260201T000000Z",
                "schema_version": 2,
                "project_slug": snapshot_project_key(discovery.slug_for(str(project_b))),
                "effective": {"model": "haiku"},
                "effective_provenance": {},
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "apply", "--set", "model=haiku", "--scope", "repo", "--project-dir", str(project_a),
            "--config-dir", str(config_dir), "--dry-run",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Already overridden by a higher-precedence layer" in out
    assert "model: already set by" in out


def test_cmd_apply_dry_run_exits_nonzero_when_plan_would_be_refused(tmp_path, capsys):
    """Fix S1: a --dry-run whose real apply would refuse (here: a
    profile naming an agent with no corresponding file, and no
    --force) must say so and exit non-zero, not print a clean diff and
    exit 0 as if the apply would succeed."""
    config_dir = tmp_path / "claudeglass"
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, agents={"ghost": {"model": "opus"}})

    exit_code = cli.main(["apply", str(profile_path), "--config-dir", str(config_dir), "--dry-run"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "would be refused" in err
    assert "no agent file found" in err


def test_baseline_list_and_show_round_trip(tmp_path, monkeypatch, capsys):
    projects_root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(projects_root, "proj-a")

    exit_code = cli.main(
        [
            "baseline",
            "--finalise",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(projects_root),
            "--project",
            "proj-a",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    list_exit_code = cli.main(["baseline", "--list", "--config-dir", str(config_dir)])
    list_out = capsys.readouterr().out
    assert list_exit_code == 0
    lines = [line for line in list_out.splitlines() if line.strip()]
    assert len(lines) == 1
    baseline_id = lines[0].split()[0]

    show_exit_code = cli.main(["baseline", "--show", baseline_id, "--config-dir", str(config_dir)])
    show_out = capsys.readouterr().out
    assert show_exit_code == 0
    assert "# Onboarding baseline report" in show_out
    assert "## Suggested profile" in show_out
    assert_privacy({"out": show_out})


def test_backtest_reports_no_predictions_without_a_store(tmp_path, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    exit_code = cli.main(["backtest", "--config-dir", str(config_dir)])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "no predictions logged yet" in err


def test_backtest_lists_judged_and_pending_predictions(tmp_path, capsys):
    from claudeglass.service.serve import STORE_FILENAME
    from claudeglass.service.store import Store

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = Store(config_dir / STORE_FILENAME)
    store.open()
    store.upsert_prediction(
        prediction_id="pred-judged",
        ts="2026-09-20T09:00:00Z",
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )
    store.judge_prediction("pred-judged", change_ts="2026-09-21T09:00:00Z", verdict="as_estimated", measured_usd=1.1, measured_pct=-10.0)
    store.upsert_prediction(
        prediction_id="pred-pending",
        ts="2026-09-22T09:00:00Z",
        source="whatif",
        measure_key="promptCacheTtl",
        agent=None,
        predicted_usd=0.5,
        predicted_pct=None,
        fidelity="simulated",
    )
    store.close()

    exit_code = cli.main(["backtest", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "model" in out and "as_estimated" in out
    assert "1 prediction still waiting on a match or more data." in out
    assert_privacy({"out": out})


def test_no_argv_with_no_data_exits_1(capsys):
    # The autouse fixture points HOME/CLAUDE_CONFIG_DIR at an empty tmp
    # dir, so the default "report" subcommand's default project (this
    # process's own cwd slug) matches nothing.
    exit_code = cli.main([])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "report" in err


# -- pricing-check / snapshot-config (unchanged from WP2/WP7) ---------------


def test_pricing_check_exits_0(capsys):
    exit_code = cli.main(["pricing-check"])
    assert exit_code == 0
    capsys.readouterr()


def test_pricing_check_models_flags_a_closest_match_resolution(capsys):
    # "claude-opus-4-1-preview" only resolves via the prefix-match step
    # against the packaged "claude-opus-4-1" id -- fix 2's approximate
    # marker must show up in --models output for it, but not for an
    # exact hit in the same call.
    exit_code = cli.main(["pricing-check", "--models", "claude-opus-4-1-preview,claude-sonnet-5"])
    assert exit_code == 0
    out = capsys.readouterr().out
    preview_line = next(line for line in out.splitlines() if "claude-opus-4-1-preview" in line)
    sonnet_line = next(line for line in out.splitlines() if line.strip().startswith("claude-sonnet-5"))
    assert "(closest match, not this model's own rate)" in preview_line
    assert "(closest match, not this model's own rate)" not in sonnet_line


def test_snapshot_config_print_hook_exits_0(capsys):
    exit_code = cli.main(["snapshot-config", "--print-hook"])
    assert exit_code == 0
    capsys.readouterr()


# -- snapshot-config --project-dir / probe-config (schema 2) ----------------
#
# --project-dir (not --project) is deliberate: the common parser already
# defines a repeatable --project meaning "a project slug to filter a
# report by" (see _build_common_parser); a single directory-path override
# for these two subcommands needed its own name to avoid a silent
# argparse option-string collision (both flags are otherwise attached to
# every subcommand via parents=[common]).


def test_snapshot_config_project_dir_runs_the_hook_for_an_explicit_directory(tmp_path, capsys):
    project = tmp_path / "some-project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus"}), encoding="utf-8"
    )
    config_dir = tmp_path / "claudeglass"

    exit_code = cli.main(
        [
            "snapshot-config",
            "--config-dir",
            str(config_dir),
            "--project-dir",
            str(project),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    snapshot_path = Path(out.strip())
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["project_settings"]
    project_entry = next(iter(snapshot["project_settings"].values()))
    assert project_entry["model"] == "opus"


def test_snapshot_config_without_project_dir_uses_cwd(tmp_path, capsys, monkeypatch):
    project = tmp_path / "cwd-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "claudeglass"
    monkeypatch.chdir(project)

    exit_code = cli.main(["snapshot-config", "--config-dir", str(config_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    snapshot = json.loads(Path(out.strip()).read_text(encoding="utf-8"))
    # cwd_hash is a hash, not the raw path -- just confirm a snapshot was
    # actually produced for *some* cwd rather than failing outright.
    assert snapshot["cwd_hash"].startswith("sha256:")


def test_probe_config_renders_markdown_with_no_raw_paths(tmp_path, capsys):
    project = tmp_path / "probe-project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet", "effortLevel": "high"}), encoding="utf-8"
    )
    config_dir = tmp_path / "claudeglass"

    exit_code = cli.main(
        [
            "probe-config",
            "--config-dir",
            str(config_dir),
            "--project-dir",
            str(project),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out

    assert out.startswith("# Config probe:")
    assert "## Settings layers" in out
    assert "## Effective config" in out
    assert "| model | sonnet |" in out
    assert "project_shared" in out  # the layer name, not a raw path

    assert_privacy(out)
    # Never write a snapshot file to disk -- probe-config only prints.
    assert not (config_dir / "snapshots").exists()


def test_probe_config_defaults_to_the_current_directory(tmp_path, capsys, monkeypatch):
    project = tmp_path / "cwd-probe-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "claudeglass"
    monkeypatch.chdir(project)

    exit_code = cli.main(["probe-config", "--config-dir", str(config_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Config probe:")


def test_probe_config_no_effective_keys_notes_it(tmp_path, capsys):
    project = tmp_path / "empty-project"
    project.mkdir(parents=True)
    config_dir = tmp_path / "claudeglass"

    exit_code = cli.main(
        ["probe-config", "--config-dir", str(config_dir), "--project-dir", str(project)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no settings layer defines any allowlisted key" in out


# -- report / sessions / recache / ttl / compactions -------------------------


def test_report_renders_markdown(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith("# ClaudeGlass report")
    assert "## Overview" in out
    assert "## Diagnostics" in out


# -- v0.3 Task 2: report --baseline <id|latest> -----------------------------


def test_report_baseline_latest_adds_baseline_comparison_section(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a")

    baseline_exit = cli.main(
        ["baseline", "--finalise", "--config-dir", str(config_dir), "--projects-root", str(root), "--project", "proj-a"]
    )
    assert baseline_exit == 0
    capsys.readouterr()

    exit_code = cli.main(
        ["report", "--config-dir", str(config_dir), "--projects-root", str(root), "--project", "proj-a", "--baseline", "latest"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## Before and after" in out
    assert "Cost per session" in out
    assert "Observed, not controlled" in out


def test_report_baseline_explicit_id_matches_latest(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a")

    cli.main(
        ["baseline", "--finalise", "--config-dir", str(config_dir), "--projects-root", str(root), "--project", "proj-a"]
    )
    capsys.readouterr()
    baseline_id = baseline_mod.list_baselines(config_dir)[-1]["id"]

    exit_code = cli.main(
        [
            "report",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--baseline",
            baseline_id,
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"your baseline {baseline_id}" in out


def test_report_baseline_unresolved_id_omits_section_and_notes_how_to_fix(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a")

    exit_code = cli.main(
        [
            "report",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--baseline",
            "does-not-exist",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## Before and after" not in out
    assert "no such baseline was found" in out
    assert "claudeglass baseline --list" in out


def test_report_baseline_latest_with_none_saved_yet_omits_section(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a")

    exit_code = cli.main(
        ["report", "--config-dir", str(config_dir), "--projects-root", str(root), "--project", "proj-a", "--baseline", "latest"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## Before and after" not in out
    assert "no baseline has been saved yet" in out


def test_report_without_baseline_flag_has_no_note_or_section(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a")

    exit_code = cli.main(["report", "--config-dir", str(config_dir), "--projects-root", str(root), "--project", "proj-a"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## Before and after" not in out
    # No --baseline flag given at all -- no resolution attempted, so no
    # "no baseline found"/"run `claudeglass baseline`" note either
    # (unlike test_report_baseline_latest_with_none_saved_yet_omits_section,
    # where --baseline latest IS given but resolves to nothing).
    assert "no such baseline" not in out
    assert "no baseline has been saved yet" not in out


# -- v0.3 Task 1: export --aggregate / import / team-report ----------------


def test_export_aggregate_writes_a_team_document(tmp_path, capsys):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a", n_turns=6)

    out_path = tmp_path / "agg.json"
    exit_code = cli.main(
        [
            "export",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--aggregate",
            "--out",
            str(out_path),
        ]
    )
    assert exit_code == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    for key in ("tool_version", "generated_at", "machine_id", "window", "scorecard"):
        assert key in doc
    for axis in ("archetype", "mode", "purpose", "agent_type", "model"):
        assert f"by_{axis}" in doc
    assert "projects" not in doc
    serialised = json.dumps(doc)
    assert "proj-a" not in serialised
    assert_privacy(doc)


def test_export_aggregate_include_projects_adds_hashed_slugs(tmp_path):
    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    _write_project(root, "proj-a", n_turns=6)

    out_path = tmp_path / "agg.json"
    exit_code = cli.main(
        [
            "export",
            "--config-dir",
            str(config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--aggregate",
            "--include-projects",
            "--out",
            str(out_path),
        ]
    )
    assert exit_code == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert "projects" in doc
    assert doc["projects"]
    for value in doc["projects"]:
        assert "proj-a" not in value


def test_import_then_team_report_round_trip(tmp_path, capsys):
    root = tmp_path / "projects"
    export_config_dir = tmp_path / "config_export"
    _write_project(root, "proj-a", n_turns=6)

    agg_path = tmp_path / "agg.json"
    cli.main(
        [
            "export",
            "--config-dir",
            str(export_config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--aggregate",
            "--out",
            str(agg_path),
        ]
    )
    capsys.readouterr()

    team_config_dir = tmp_path / "config_team"
    import_exit = cli.main(["import", str(agg_path), "--config-dir", str(team_config_dir)])
    out = capsys.readouterr().out
    assert import_exit == 0
    assert "Imported" in out
    saved = list((team_config_dir / "team").glob("*.json"))
    assert len(saved) == 1

    report_exit = cli.main(["team-report", "--config-dir", str(team_config_dir)])
    out = capsys.readouterr().out
    assert report_exit == 0
    assert "## Team report" in out
    assert "Team comparison: archetype" in out
    assert "Team comparison: agent type" in out
    assert "Observed, not controlled" in out


def test_import_rejects_a_document_with_a_disallowed_key(tmp_path, capsys):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(
        json.dumps(
            {
                "tool_version": "0.2.0",
                "generated_at": "2026-01-01T00:00:00.000Z",
                "machine_id": "abc123abc123",
                "window": "last 7 days",
                "session_id": "leaked",
            }
        ),
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    exit_code = cli.main(["import", str(bad_path), "--config-dir", str(config_dir)])
    out = capsys.readouterr().err
    assert exit_code == 2
    assert "rejected" in out
    assert not (config_dir / "team").exists()


def test_import_rejects_invalid_json(tmp_path, capsys):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    config_dir = tmp_path / "config"
    exit_code = cli.main(["import", str(bad_path), "--config-dir", str(config_dir)])
    out = capsys.readouterr().err
    assert exit_code == 2
    assert "not valid JSON" in out


def test_team_report_with_no_imported_documents_exits_1(tmp_path, capsys):
    config_dir = tmp_path / "config"
    exit_code = cli.main(["team-report", "--config-dir", str(config_dir)])
    out = capsys.readouterr().err
    assert exit_code == 1
    assert "run `claudeglass import" in out


def test_team_report_json_output(tmp_path, capsys):
    root = tmp_path / "projects"
    export_config_dir = tmp_path / "config_export"
    _write_project(root, "proj-a", n_turns=6)

    agg_path = tmp_path / "agg.json"
    cli.main(
        [
            "export",
            "--config-dir",
            str(export_config_dir),
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--aggregate",
            "--out",
            str(agg_path),
        ]
    )
    capsys.readouterr()

    team_config_dir = tmp_path / "config_team"
    cli.main(["import", str(agg_path), "--config-dir", str(team_config_dir)])
    capsys.readouterr()

    exit_code = cli.main(["team-report", "--config-dir", str(team_config_dir), "--json"])
    out = capsys.readouterr().out
    assert exit_code == 0
    payload = json.loads(out)
    section = next(s for s in payload["report"]["sections"] if s["key"] == "team_report")
    assert section["tables"]


def test_allow_titles_flag_was_removed(capsys):
    # Fix R17: --allow-titles implied a privacy control that never
    # existed (report.py's own docstring says the keyword it still
    # accepts is a permanent no-op -- nothing captures title text to
    # gate) -- it must no longer be a recognised CLI flag at all.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--allow-titles"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--allow-titles" in err


# -- --quiet / --verbose (Fix R21) ------------------------------------------


def test_quiet_and_verbose_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--quiet", "--verbose"])
    assert exc_info.value.code == 2


def test_verbose_prints_corpus_stats_to_stderr(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--verbose"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "[corpus]" in err


def test_default_verbosity_omits_corpus_stats(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "[corpus]" not in err


def test_load_corpus_for_args_quiet_suppresses_stats_even_if_verbose_is_also_set(tmp_path, capsys):
    # Fix R21: --quiet was accepted by argparse but never actually
    # consulted anywhere in the code -- a silent no-op. argparse's own
    # mutual-exclusion check keeps a human from passing both flags at
    # once (covered above), but _load_corpus_for_args's own contract
    # must not lean on that alone: build a Namespace with both set (as
    # a caller bypassing argparse could) and confirm --quiet still wins.
    root = tmp_path / "projects"
    project_dir = _write_project(root, "proj-a")
    config = cli.load_config(None)
    args = argparse.Namespace(
        no_cache=True,
        rebuild_cache=False,
        days=None,
        since=None,
        until=None,
        limit=None,
        window_by="mtime",
        jobs=1,
        verbose=True,
        quiet=True,
    )
    cli._load_corpus_for_args(args, config, root, [project_dir])
    err = capsys.readouterr().err
    assert "[corpus]" not in err


@pytest.mark.parametrize(
    "command,section_title",
    [
        ("sessions", "## Sessions"),
        ("recache", "## Re-cache"),
        ("ttl", "## TTL"),
        ("limits", "## Usage limits"),
        ("carry", "## Context carry cost per tool"),
        ("compaction-sim", "## Compaction-window sweep"),
        ("model-swap", "## Model-swap counterfactual"),
        ("waste", "## Wasted-turn spend"),
        ("compactions", "## Compactions"),
    ],
)
def test_focused_subcommands_render_overview_plus_their_own_section(tmp_path, capsys, command, section_title):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main([command, "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "overview" in section_keys
    assert cli._REPORT_LIKE_SECTIONS[command] in section_keys
    # No other report section should have leaked in via `include`.
    assert set(section_keys) == {"overview", cli._REPORT_LIKE_SECTIONS[command]}


def test_report_json_has_expected_top_level_keys(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload.keys()) >= {"schema_version", "tool_version", "report"}
    report = payload["report"]
    assert set(report.keys()) >= {"meta", "sections", "recommendations", "diagnostics"}
    assert isinstance(report["sections"], list) and report["sections"]


def test_report_csv_dir_writes_one_csv_per_table_plus_index(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    csv_dir = tmp_path / "csvs"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--csv-dir", str(csv_dir)]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert (csv_dir / "_index.csv").exists()
    assert (csv_dir / "_meta.csv").exists()
    assert any(csv_dir.glob("overview__*.csv"))


def test_report_html_writes_a_file_with_no_external_references(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    html_path = tmp_path / "report.html"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--html", str(html_path)]
    )
    assert exit_code == 0
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html


def _force_patch_set_text(monkeypatch, text: str = "--- a/settings.json\n+++ b/settings.json\n") -> None:
    """Force ``recommend.render_patch_set`` to return ``text`` regardless
    of whether the tiny synthetic fixture actually earns any
    recommendations -- the patch-set-placement tests below care about
    *where* the text lands for each output mode, not about which
    recommendation rules fire.
    """
    from claudeglass import recommend

    monkeypatch.setattr(recommend, "render_patch_set", lambda recs: text)


def test_report_json_patch_set_is_embedded_as_a_json_key_not_appended(tmp_path, capsys, monkeypatch):
    # Fix cli/patch-set-json: `--json --patch-set` used to print the
    # patch-set text as trailing lines after the JSON blob, so
    # `json.loads` on stdout would raise. It must now be valid JSON
    # with the patch set embedded under a top-level "patch_set" key,
    # and nothing else printed to stdout.
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--json", "--patch-set"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # would raise if anything trailed the JSON blob
    assert payload["patch_set"] == patch_text


def test_report_json_without_patch_set_flag_omits_the_key(tmp_path, capsys, monkeypatch):
    _force_patch_set_text(monkeypatch)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "patch_set" not in payload


def test_report_html_patch_set_writes_a_sibling_file(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    html_path = tmp_path / "out" / "report.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--html", str(html_path), "--patch-set"]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert html_path.exists()
    assert (html_path.parent / "patch-set.txt").read_text(encoding="utf-8") == patch_text


def test_report_csv_dir_patch_set_writes_a_file_in_the_dir(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    csv_dir = tmp_path / "csvs"
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--csv-dir", str(csv_dir), "--patch-set"]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert (csv_dir / "patch-set.txt").read_text(encoding="utf-8") == patch_text


def test_report_markdown_patch_set_keeps_appending_to_stdout(tmp_path, capsys, monkeypatch):
    patch_text = "--- a/settings.json\n+++ b/settings.json\n"
    _force_patch_set_text(monkeypatch, patch_text)
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--patch-set"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.rstrip("\n").endswith(patch_text.rstrip("\n"))


def test_report_patch_set_flag_with_no_recommendations_prints_nothing_extra(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")

    baseline_exit = cli.main(["report", "--projects-root", str(root), "--project", "proj-a"])
    assert baseline_exit == 0
    baseline_out = _strip_generated_at(capsys.readouterr().out)

    _force_patch_set_text(monkeypatch, "")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--patch-set"]
    )
    assert exit_code == 0
    out = _strip_generated_at(capsys.readouterr().out)
    assert out == baseline_out


def test_report_phases_flag_adds_phases_section(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--phases", "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "phases" in section_keys


def test_report_without_phases_flag_omits_phases_section(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(["report", "--projects-root", str(root), "--project", "proj-a", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    section_keys = [s["key"] for s in payload["report"]["sections"]]
    assert "phases" not in section_keys


def test_empty_window_exits_1_with_reason_naming_root(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "does-not-exist"]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert str(root) in err


def test_bad_pricing_path_exits_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    missing_pricing = tmp_path / "no-such-pricing.toml"
    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--pricing",
            str(missing_pricing),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "pricing" in err.lower()


def test_jobs_1_and_jobs_2_give_byte_identical_markdown(tmp_path, capsys):
    root = tmp_path / "projects"
    for i in range(3):
        _write_project(root, f"proj-{i}")

    outputs = []
    for jobs in (1, 2):
        exit_code = cli.main(
            [
                "report",
                "--projects-root",
                str(root),
                "--all-projects",
                "--jobs",
                str(jobs),
                "--no-cache",
            ]
        )
        assert exit_code == 0
        outputs.append(_strip_generated_at(capsys.readouterr().out))

    assert outputs[0] == outputs[1]


def test_no_cache_and_warm_cache_give_byte_identical_output(tmp_path, capsys):
    root = tmp_path / "projects"
    # Backdate the file so the digest cache doesn't treat it as a live
    # session (cache.py: mtime < 60s is always a miss).
    _write_project(root, "proj-a", age_seconds=120)
    config_dir = tmp_path / "claudeglass"

    base_args = [
        "report",
        "--projects-root",
        str(root),
        "--project",
        "proj-a",
        "--config-dir",
        str(config_dir),
    ]

    exit_code = cli.main(base_args + ["--no-cache"])
    assert exit_code == 0
    no_cache_output = _strip_generated_at(capsys.readouterr().out)

    # Cold cache: config_dir/cache/ doesn't exist yet, this writes it.
    exit_code = cli.main(base_args)
    assert exit_code == 0
    cold_output = _strip_generated_at(capsys.readouterr().out)

    # Warm cache: every entry from the previous run is now a hit.
    exit_code = cli.main(base_args)
    assert exit_code == 0
    warm_output = _strip_generated_at(capsys.readouterr().out)

    assert no_cache_output == cold_output
    assert cold_output == warm_output


# -- config-diff --------------------------------------------------------


def test_config_diff_renders_a_table(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "claude-home" / ".claude"
    snapshots_dir = config_dir / "claudeglass" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir / "claudeglass"),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Sessions by setting: model (your settings)" in out
    assert "Sessions" in out


def test_config_diff_finds_snapshots_written_by_the_hook_at_the_same_config_dir(tmp_path, capsys):
    """Fix config-dir: an explicit --config-dir now means the same thing
    everywhere -- the claudeglass directory itself, with snapshots
    directly under it (see hooks/snapshot-config.py's
    resolve_config_dir and snapshots.load_snapshots docstrings). A user
    who points the *same* --config-dir value at both `snapshot-config`
    and `config-diff` -- the natural thing to try, and previously
    broken (fix R16 papered over it with a dual-fallback that this fix
    removes in favour of one real convention) -- must have config-diff
    find exactly what the hook wrote.
    """
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "my-custom-claudeglass"
    snapshots_dir = config_dir / "snapshots"  # the hook's own layout for this same --config-dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Sessions by setting: model (your settings)" in out
    assert "Sessions" in out


def test_snapshot_config_hook_and_config_diff_agree_on_the_same_config_dir(tmp_path, capsys):
    """End-to-end regression for fix config-dir: run the real
    snapshot-config hook (as a subprocess, the way it's actually
    invoked -- see tests/test_hook.py) against a --config-dir, then run
    config-diff against that exact same --config-dir and confirm it
    picks up the snapshot the hook just wrote. This is the concrete
    "point the same --config-dir at both" scenario the reconciliation
    fixes.
    """
    hook_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "claudeglass"
        / "hooks"
        / "snapshot-config.py"
    )
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet"}), encoding="utf-8"
    )
    config_dir = home / ".claude" / "claudeglass"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    hook_result = subprocess.run(
        [sys.executable, str(hook_path), "--config-dir", str(config_dir)],
        input=json.dumps({"session_id": "s1"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert hook_result.returncode == 0

    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)
    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Sessions by setting: model (your settings)" in out


def test_config_diff_honors_session_overrides(tmp_path, capsys, monkeypatch):
    """Fix R18: _build_session_metrics used to hardcode {} for
    classify.classify_session's overrides argument, so a manual
    sessions.toml mode/purpose correction -- honoured by every other
    report-like subcommand via _cmd_report_like's own
    load_session_overrides(config_dir) -- was silently dropped for
    config-diff alone. Spy on classify.classify_session to confirm
    config-diff now actually loads and threads sessions.toml through.
    """
    root = tmp_path / "projects"
    _write_project(root, "proj-diff", age_seconds=120)

    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir = config_dir.parent / "claudeglass" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "20200101T000000Z.json").write_text(
        json.dumps({"ts": "20200101T000000Z", "user_settings": {"model": "sonnet"}}),
        encoding="utf-8",
    )
    (snapshots_dir / "20200201T000000Z.json").write_text(
        json.dumps({"ts": "20200201T000000Z", "user_settings": {"model": "fable"}}),
        encoding="utf-8",
    )
    (config_dir / "sessions.toml").write_text(
        '[sessions."some-session-id"]\nmode = "auto"\n', encoding="utf-8"
    )

    seen_overrides: list[dict] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_overrides.append(overrides)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        [
            "config-diff",
            "--projects-root",
            str(root),
            "--project",
            "proj-diff",
            "--config-dir",
            str(config_dir),
            "--key",
            "user_settings.model",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_overrides, "classify_session was never called"
    assert all(o == {"some-session-id": {"mode": "auto"}} for o in seen_overrides)


def test_config_diff_requires_key_or_auto_keys(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["config-diff", "--projects-root", str(root), "--project", "proj-a"])
    assert exc_info.value.code == 2


# -- ScorecardError surfaced as a clean exit-2 error (Fix R20) --------------


def test_report_exits_2_with_clean_message_on_misordered_scorecard_thresholds(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        "[thresholds.scorecard]\n"
        "cache_recache_share_pct = [50.0, 30.0, 15.0, 5.0]\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--config-dir",
            str(config_dir),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "cache_recache_share_pct" in lines[0]
    assert "Traceback" not in err


# -- --tz (Fix R24) ----------------------------------------------------------


def test_tz_flag_overrides_config_toml_for_this_run(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('tz = "UTC"\n', encoding="utf-8")

    seen_tz: list[str | None] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_tz.append(tz)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--config-dir",
            str(config_dir),
            "--tz",
            "America/New_York",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_tz, "classify_session was never called"
    assert all(tz == "America/New_York" for tz in seen_tz)


def test_tz_flag_defaults_to_config_toml_value_when_absent(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('tz = "UTC"\n', encoding="utf-8")

    seen_tz: list[str | None] = []
    real_classify_session = cli.classify.classify_session

    def _spy(top, subs, overrides, tz, **kwargs):
        seen_tz.append(tz)
        return real_classify_session(top, subs, overrides, tz, **kwargs)

    monkeypatch.setattr(cli.classify, "classify_session", _spy)

    exit_code = cli.main(
        ["report", "--projects-root", str(root), "--project", "proj-a", "--config-dir", str(config_dir)]
    )
    assert exit_code == 0
    capsys.readouterr()
    assert seen_tz and all(tz == "UTC" for tz in seen_tz)


@pytest.mark.skipif(
    not zoneinfo.available_timezones(),
    reason="no tz database on this machine (see classify.py's module docstring) -- "
    "--tz can't be validated against anything here, so it's accepted uncontested",
)
def test_tz_flag_rejects_an_unknown_zone_with_a_clean_exit_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_project(root, "proj-a")

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--tz",
            "Not/A_Real_Zone",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "Not/A_Real_Zone" in lines[0]
    assert "Traceback" not in err


def test_tz_flag_is_accepted_uncontested_when_machine_has_no_tz_database(tmp_path, capsys, monkeypatch):
    # The inverse of the skipped test above: force the "no tz database"
    # branch regardless of what this machine actually has, and confirm
    # a clearly-bogus zone name is still accepted (degrading later to
    # local time inside classify._to_local, exactly like an unresolvable
    # config.toml value already does) rather than rejected.
    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    monkeypatch.setattr(cli, "available_timezones", lambda: frozenset())

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(root),
            "--project",
            "proj-a",
            "--tz",
            "Not/A_Real_Zone",
        ]
    )
    assert exit_code == 0


# -- probe ----------------------------------------------------------------


def test_probe_command_output_has_no_string_longer_than_64_chars(tmp_path, capsys):
    root = tmp_path / "projects"
    project_dir = root / "proj-probe"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-1.jsonl",
        [
            turn_line(),
            {"type": "attachment", "attachment": {"type": "y" * 200}, "uuid": "u1"},
            {"type": "system", "subtype": "s" * 200, "uuid": "u2"},
        ],
    )
    exit_code = cli.main(["probe", "--projects-root", str(root), "--project", "proj-probe"])
    assert exit_code == 0
    out = capsys.readouterr().out
    for line in out.splitlines():
        if not line.startswith("- "):
            continue
        token = line[2:].rsplit(": ", 1)[0]
        assert len(token) <= 64, (line, len(token))


def test_probe_single_file(tmp_path, capsys):
    path = tmp_path / "one.jsonl"
    write_jsonl(path, [turn_line(), turn_line()])
    exit_code = cli.main(["probe", "--file", str(path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "files: 1" in out
    assert "assistant: 2" in out


def test_probe_missing_file_exits_2(tmp_path, capsys):
    exit_code = cli.main(["probe", "--file", str(tmp_path / "missing.jsonl")])
    assert exit_code == 2


# -- real fixture (skipped if absent) ---------------------------------------


_REAL_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "real"

pytestmark_real_fixture = pytest.mark.skipif(
    not (_REAL_FIXTURE_ROOT / "session-a").exists()
    or not any((_REAL_FIXTURE_ROOT / "session-a").glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)


@pytestmark_real_fixture
def test_report_against_the_real_fixture_exits_0_with_nonempty_sections(capsys):
    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(_REAL_FIXTURE_ROOT),
            "--project",
            "session-a",
        ]
    )
    assert exit_code == 0
    markdown = capsys.readouterr().out
    assert "## Overview" in markdown
    assert "## Sessions" in markdown
    assert "## Cache rebuilds" in markdown
    assert "## Cache lifetime (TTL)" in markdown

    exit_code = cli.main(
        [
            "report",
            "--projects-root",
            str(_REAL_FIXTURE_ROOT),
            "--project",
            "session-a",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["sections"]


# -- python -m entry point / zipapp exit-code propagation -------------------


def test_python_dash_m_version_exits_0():
    result = subprocess.run(
        [sys.executable, "-m", "claudeglass", "--version"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_python_dash_m_bad_input_exits_2():
    # v0.3: init/baseline are real subcommands now, so there is no
    # longer an actually-unimplemented one to name here -- "scrub-
    # fixture" with none of its required flags is a real, always
    # available bad-input case instead (see module docstring).
    result = subprocess.run(
        [sys.executable, "-m", "claudeglass", "scrub-fixture"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    assert result.returncode == 2
    assert "scrub-fixture" in result.stderr


def test_statusline_cli_forwards_config_dir_flag(tmp_path, monkeypatch, capsys):
    """``claudeglass statusline --config-dir PATH`` must actually
    write there. A v0.2 release bug: ``_cmd_statusline`` parsed
    ``--config-dir`` via the "common" argparse group (it's in ``--help``
    for every subcommand) but never forwarded it to
    ``statusline.main()``, which always fell back to
    ``$CLAUDE_CONFIG_DIR``/``~/.claude`` -- silently writing
    ``usage-log.csv``/``statusline-keys.json`` to the real config dir
    even when a caller explicitly asked for an isolated one.
    """
    explicit_config_dir = tmp_path / "explicit-dir"
    payload = {"rate_limits": {"five_hour": {"used_percentage": 7}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = cli.main(["statusline", "--config-dir", str(explicit_config_dir)])

    assert rc == 0
    assert (explicit_config_dir / "usage-log.csv").exists()


def test_cmd_apply_set_explains_the_change_then_reverts(tmp_path, capsys):
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    settings = claude_root / "settings.json"
    settings.write_text('{"effortLevel": "high"}', encoding="utf-8")
    base = ["apply", "--set", "effortLevel=medium", "--config-dir", str(config_dir), "--claude-root", str(claude_root),
            "--yes"]

    assert cli.main([*base, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Change: effortLevel" in out
    assert "What it controls: How hard Claude thinks" in out
    assert "Now: high. After: medium." in out
    assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "high"}

    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "Change: effortLevel" in out
    ts = re.search(r"To revert: claudeglass apply --revert (\S+)", out).group(1)
    assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "medium"}

    settings.write_text('{"effortLevel": "low"}', encoding="utf-8")
    assert cli.main(["apply", "--revert", ts, "--config-dir", str(config_dir)]) == 2
    assert "--ignore-changes" in capsys.readouterr().err
    assert cli.main(["apply", "--revert", ts, "--ignore-changes", "--config-dir", str(config_dir)]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "high"}


def test_cmd_apply_shows_the_change_and_asks_first(tmp_path, capsys, monkeypatch):
    # It changes how Claude works for every project: never without a yes.
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    settings = claude_root / "settings.json"
    settings.write_text('{"effortLevel": "high"}', encoding="utf-8")
    command = ["apply", "--set", "effortLevel=medium", "--config-dir", str(config_dir), "--claude-root", str(claude_root)]
    for answer in ("n\n", ""):  # no, or nobody there to answer
        monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
        assert cli.main(command) == 1
        out = capsys.readouterr().out
        assert '"effortLevel": "medium"' in out and "Apply these changes?" in out and "Nothing changed" in out
        assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "high"}
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert cli.main(command) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "medium"}


def test_cmd_apply_never_overwrites_a_file_changed_while_it_asked(tmp_path, capsys, monkeypatch):
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    settings = claude_root / "settings.json"
    settings.write_text('{"effortLevel": "high"}', encoding="utf-8")

    class Answer(io.StringIO):
        def readline(self, *args):
            # Claude Code allows a tool for good while the question waits.
            settings.write_text('{"effortLevel": "high", "permissions": {"allow": ["Bash(npm test)"]}}', encoding="utf-8")
            return "y\n"

    monkeypatch.setattr(sys, "stdin", Answer())
    command = ["apply", "--set", "effortLevel=medium", "--config-dir", str(config_dir), "--claude-root", str(claude_root)]
    assert cli.main(command) == 1
    assert "run the command again" in capsys.readouterr().err
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"] == {"allow": ["Bash(npm test)"]}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_cmd_apply_writes_through_a_symlinked_settings_file(tmp_path):
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    dotfiles = tmp_path / "dotfiles" / "claude-settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text('{"effortLevel": "high"}', encoding="utf-8")
    dotfiles.chmod(0o600)
    (claude_root / "settings.json").symlink_to(dotfiles)
    command = ["apply", "--set", "effortLevel=medium", "--config-dir", str(config_dir), "--claude-root",
               str(claude_root), "--yes"]
    assert cli.main(command) == 0
    assert (claude_root / "settings.json").is_symlink()
    assert json.loads(dotfiles.read_text(encoding="utf-8")) == {"effortLevel": "medium"}
    assert dotfiles.stat().st_mode & 0o777 == 0o600


def test_cmd_apply_set_merges_a_skill_override_by_name(tmp_path, capsys):
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    settings = claude_root / "settings.json"
    settings.write_text('{"skillOverrides": {"pdf": "off"}}', encoding="utf-8")
    command = [
        "apply", "--set", "skillOverrides=impeccable:impeccable:name-only,xlsx:user-invocable-only",
        "--config-dir", str(config_dir), "--claude-root", str(claude_root),
    ]
    assert cli.main([*command, "--dry-run"]) == 0
    assert "skillOverrides" in capsys.readouterr().out
    assert cli.main([*command, "--yes"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8"))["skillOverrides"] == {
        "pdf": "off",
        "impeccable:impeccable": "name-only",
        "xlsx": "user-invocable-only",
    }
    assert cli.main(["apply", "--set", "skillOverrides=pdf:sometimes", "--config-dir", str(config_dir),
                     "--claude-root", str(claude_root), "--dry-run"]) == 2


def test_cmd_apply_set_env_writes_the_settings_env_block(tmp_path, capsys):
    # COV-07/COV-11: apply --set env.NAME=value writes into the target
    # settings file's own "env" object, exactly like any other --set key
    # -- no more "export this yourself" guidance.
    claude_root = tmp_path / "claude"
    config_dir = tmp_path / "tl"
    claude_root.mkdir()
    settings = claude_root / "settings.json"
    settings.write_text('{"env": {"SOME_OTHER_VAR": "keep-me"}}', encoding="utf-8")
    command = [
        "apply", "--set", "env.ENABLE_TOOL_SEARCH=true",
        "--config-dir", str(config_dir), "--claude-root", str(claude_root),
    ]
    assert cli.main([*command, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Change: env.ENABLE_TOOL_SEARCH" in out
    assert '"ENABLE_TOOL_SEARCH": "true"' in out
    assert "export" not in out.lower()
    assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {"SOME_OTHER_VAR": "keep-me"}}

    assert cli.main([*command, "--yes"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "env": {"SOME_OTHER_VAR": "keep-me", "ENABLE_TOOL_SEARCH": "true"}
    }


def test_cmd_apply_set_rejects_mixing_env_and_settings_keys(tmp_path, capsys):
    config_dir = tmp_path / "tl"
    claude_root = tmp_path / "claude"
    exit_code = cli.main(
        [
            "apply", "--set", "env.ENABLE_TOOL_SEARCH=true", "--set", "effortLevel=medium",
            "--config-dir", str(config_dir), "--claude-root", str(claude_root), "--dry-run",
        ]
    )
    assert exit_code == 2
    assert "can't be mixed" in capsys.readouterr().err


def test_cmd_apply_set_env_rejects_agent_flag(tmp_path, capsys):
    config_dir = tmp_path / "tl"
    claude_root = tmp_path / "claude"
    exit_code = cli.main(
        [
            "apply", "--set", "env.ENABLE_TOOL_SEARCH=true", "--agent", "reviewer",
            "--config-dir", str(config_dir), "--claude-root", str(claude_root), "--dry-run",
        ]
    )
    assert exit_code == 2
    assert "doesn't apply to an environment variable" in capsys.readouterr().err


def test_cmd_apply_launch_prints_effort_flag_when_profile_sets_effort_level(tmp_path, capsys):
    # PROF-02: --launch --dry-run prints `claude --settings <file> --effort
    # <level>` for session-only use, since a launch overlay has no other
    # way to carry effortLevel along with it.
    config_dir = tmp_path / "tl"
    claude_root = tmp_path / "claude"
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, settings={"effortLevel": "high"})

    exit_code = cli.main(
        ["apply", str(profile_path), "--launch", "--config-dir", str(config_dir), "--claude-root", str(claude_root)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert re.search(r"claude --settings \S+ --effort high", out)
    assert "Session-only" in out


def test_cmd_apply_launch_omits_effort_flag_without_an_effort_level(tmp_path, capsys):
    config_dir = tmp_path / "tl"
    claude_root = tmp_path / "claude"
    profile_path = tmp_path / "sample.toml"
    _write_profile_toml(profile_path, settings={"outputStyle": "concise"})

    exit_code = cli.main(
        ["apply", str(profile_path), "--launch", "--config-dir", str(config_dir), "--claude-root", str(claude_root)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "--effort" not in out


def test_check_lists_every_quick_action_and_runs_one_in_full(tmp_path, capsys):
    from claudeglass.quick_actions import CHECK_IDS

    root = tmp_path / "projects"
    _write_project(root, "proj-a")
    base = ["--projects-root", str(root), "--project", "proj-a", "--config-dir", str(tmp_path / "tl")]
    assert cli.main(["check", *base]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Quick actions")
    assert all(f"`{check_id}`" in out for check_id in CHECK_IDS)
    assert cli.main(["check", "models", *base]) == 0
    assert capsys.readouterr().out.startswith("## Is each agent on the cheapest model")



def test_cli_reports_merge_the_dashboards_session_tags_over_sessions_toml(tmp_path):
    from claudeglass.service.store import Store

    store = Store(str(tmp_path / "service.db"))
    store.open()
    store.upsert_session(session_id="s1", project_slug="p", slug="p", first_ts="2026-09-01T00:00:00Z",
                         last_ts="2026-09-01T01:00:00Z")
    store.set_tag("s1", "purpose", "review")
    store.set_feedback("s1", outcome="missed", slow=["rework"], worth="no", helped=["plan"])
    store.close()
    overrides = {"s1": {"mode": "overnight", "purpose": "planning"}, "s2": {"mode": "mixed"}}
    merged, ratings = cli._merge_dashboard_marks(tmp_path, overrides)
    assert merged == {"s1": {"mode": "overnight", "purpose": "review"}, "s2": {"mode": "mixed"}}
    assert overrides["s1"]["purpose"] == "planning"
    assert ratings["s1"]["outcome"] == "missed" and ratings["s1"]["helped"] == ["plan"]
    assert cli._merge_dashboard_marks(tmp_path / "none", overrides) == (overrides, {})


def test_a_project_slug_may_start_with_a_dash():
    """Every slug on Linux and macOS starts with '-' (/home/alice/shop is
    -home-alice-shop); argparse alone reads it as an option."""
    args = cli._make_parser().parse_args(
        ["report", "--project", "-home-alice-shop", "--project", "C--work-docs", "--project-family", "-home-alice-"]
    )
    assert args.project == ["-home-alice-shop", "C--work-docs"]
    assert args.project_family == "-home-alice-"
    # A missing value is still an error, not the next flag taken as one.
    with pytest.raises(SystemExit):
        cli._make_parser().parse_args(["report", "--project", "--all-projects"])

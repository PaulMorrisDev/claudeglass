"""What ``tuning export``, ``tuning summary``, the CLI report and ``export``
say about the sessions of the window the dashboard's store still holds after
Claude Code deleted their transcripts (R12). The world is a temp folder
(projects root, ``--config-dir`` with a ``service.db`` one FileWatcher tick
made, an empty Claude root), never the real config. The sentences are tested
in ``tests/test_covered.py``.
"""

from __future__ import annotations

import html
import json
import shutil
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone

import pytest

from claudeglass import cli, discovery, parse, tuning
from claudeglass.config import load_config
from claudeglass.pricing import load_pricing
from claudeglass.service import rebuild
from claudeglass.service.store import Store

from helpers import fallback_read_session, store_with_gone_sessions

NOW = datetime.now(timezone.utc).replace(microsecond=0)

ONE = (
    "These figures cover 1 of 3 sessions in this window. "
    "The transcripts of the other 2 were not found, and the dashboard still has those sessions."
)


def S(session_id: str, days: float) -> list[dict]:
    return fallback_read_session(session_id, NOW - timedelta(days=days))


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"d" * 32)


def three_sessions(tmp_path, **kw):
    """The plan's world: one session on disk, two whose transcripts are gone."""
    return store_with_gone_sessions(
        tmp_path,
        {"proj-a": {"keep": S("keep", 1), "gone-a": S("gone-a", 2), "gone-b": S("gone-b", 3)}},
        kw.pop("gone", {"gone-a", "gone-b"}),
        **kw,
    )


def store_state(config_dir):
    """What a store holds, read through a fresh read-only connection: every
    table and row (so a write still sitting in ``service.db-wal`` shows), the
    ``-wal``'s size and the main file's bytes."""
    main = config_dir / "service.db"
    wal = config_dir / "service.db-wal"
    wal_size = wal.stat().st_size if wal.exists() else 0
    conn = sqlite3.connect(f"{main.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return main.read_bytes(), dump, wal_size


def drop_store(config_dir) -> None:
    for name in ("service.db", "service.db-wal", "service.db-shm"):
        (config_dir / name).unlink(missing_ok=True)


def tuning_export(world, out, *extra, capsys):
    projects, config_dir, claude_root = world
    capsys.readouterr()
    code = cli.main(
        [
            "tuning",
            "export",
            "--projects-root",
            str(projects),
            "--config-dir",
            str(config_dir),
            "--claude-root",
            str(claude_root),
            "--out",
            str(out),
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def report(world, *extra, capsys):
    projects, config_dir, _claude_root = world
    capsys.readouterr()
    code = cli.main(
        ["report", "--projects-root", str(projects), "--config-dir", str(config_dir), "--all-projects", "--days", "30", *extra]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


STORED_2 = (
    "2 of 3 sessions in this window are read from what the dashboard kept, "
    "because their transcripts were not found."
)
#: The same line as ``tuning summary`` prints it, for a file that may have come from another machine.
STORED_2_FILE = (
    "2 of 3 sessions in this window were read from what the dashboard kept "
    "on the machine that made this file. Their transcripts were not found there."
)


def test_tuning_export_covers_all_3_and_says_2_were_read_from_what_the_dashboard_kept(tmp_path, capsys):
    world = three_sessions(tmp_path)
    projects, config_dir, claude_root = world
    out = tmp_path / "figures.json"
    before = store_state(config_dir)
    code, _stdout, err = tuning_export(world, out, capsys=capsys)
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["coverage"] == {"sessions": 3, "read": 1, "stored": 2, "older": 0}
    assert list(doc).index("coverage") == 6
    assert doc["compactions"]["sessions"] == 3
    assert doc["pieces"]["total"] == 3
    assert doc["prompting"]["totals"]["typed"] == 3
    assert err == f"claudeglass tuning export: {STORED_2}\n"
    assert store_state(config_dir) == before
    capsys.readouterr()
    assert cli.main(["tuning", "summary", str(out)]) == 0
    assert capsys.readouterr().out.splitlines()[1] == STORED_2_FILE
    # The dashboard's own corpus gives the same figures.
    config = load_config(config_dir)
    since = discovery.window_start_iso(30, config.tz)
    store = Store.open_read_only(config_dir / "service.db", config_dir=config_dir)
    try:
        dash = tuning.build(
            rebuild.corpus_from_store(store, since=since),
            config,
            load_pricing(config_dir=config_dir),
            config_dir=config_dir,
            days=30,
            today=date.fromisoformat(doc["generated_on"]),
            claude_root=claude_root,
            # The export read the dashboard's card marks (none yet): an empty block, not a missing one.
            card_marks=tuning.CardMarks([], frozenset(), True),
        )
    finally:
        store.close()
    assert doc["cards"] == {"shown": 0, "rows": []}
    assert {k: v for k, v in doc.items() if k != "coverage"} == dash


def test_the_export_marks_the_gone_sessions_an_older_version_read(tmp_path, capsys):
    world = three_sessions(tmp_path, older={"gone-a", "gone-b"})
    out = tmp_path / "figures.json"
    code, _stdout, err = tuning_export(world, out, capsys=capsys)
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["coverage"]["older"] == 2 and doc["coverage"]["stored"] == 2
    older = (
        "2 of 3 sessions in this window were read by an older version and their transcripts are gone, "
        "so the newer figures leave them out."
    )
    assert err == f"claudeglass tuning export: {STORED_2}\nclaudeglass tuning export: {older}\n"
    # A figure no version changed counts them.
    assert doc["prompting"]["totals"]["typed"] == 3
    capsys.readouterr()
    assert cli.main(["tuning", "summary", str(out)]) == 0
    assert capsys.readouterr().out.splitlines()[1:3] == [STORED_2_FILE, older]


def test_no_store_or_nothing_gone_leaves_the_export_as_it_was(tmp_path, capsys):
    world = three_sessions(tmp_path / "a")
    drop_store(world[1])
    code, _stdout, err = tuning_export(world, tmp_path / "a.json", capsys=capsys)
    assert code == 0 and err == ""
    assert "coverage" not in json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))

    world = three_sessions(tmp_path / "b", gone=set())
    code, _stdout, err = tuning_export(world, tmp_path / "b.json", capsys=capsys)
    assert code == 0 and err == ""
    assert "coverage" not in json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))


def test_a_store_that_cannot_be_read_leaves_the_export_as_it_was(tmp_path, capsys):
    world = three_sessions(tmp_path)
    config_dir = world[1]
    for name in ("service.db-wal", "service.db-shm"):
        (config_dir / name).unlink(missing_ok=True)
    (config_dir / "service.db").write_bytes(b"not a database" * 64)
    code, _stdout, err = tuning_export(world, tmp_path / "figures.json", capsys=capsys)
    assert code == 0 and err == ""
    assert "coverage" not in json.loads((tmp_path / "figures.json").read_text(encoding="utf-8"))


def test_the_sentences_name_no_day_count_whatever_the_settings_say(tmp_path, capsys):
    world = three_sessions(tmp_path)
    snapshots = world[1] / "snapshots"
    snapshots.mkdir()
    (snapshots / "2026-01-01T00-00-00Z.json").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "schema_version": 2, "effective": {"cleanupPeriodDays": 45}}),
        encoding="utf-8",
    )
    code, _stdout, err = tuning_export(world, tmp_path / "figures.json", capsys=capsys)
    assert code == 0
    doc = json.loads((tmp_path / "figures.json").read_text(encoding="utf-8"))
    assert "kept_days" not in doc["coverage"]
    assert err == f"claudeglass tuning export: {STORED_2}\n"
    code, out, _err = report(world, capsys=capsys)
    assert code == 0 and "- " + ONE in out.splitlines()
    assert "45" not in ONE and "days here" not in out


def test_only_gone_sessions_in_the_window_and_the_picked_projects_count(tmp_path, capsys):
    world = store_with_gone_sessions(
        tmp_path,
        {
            "proj-a": {"keep": S("keep", 1), "near": S("near", 2), "far": S("far", 40)},
            "proj-b": {"b1": S("b1", 1)},
        },
        {"near", "far"},
    )

    def export(*extra):
        code, _stdout, _err = tuning_export(world, tmp_path / "figures.json", *extra, capsys=capsys)
        assert code == 0
        return json.loads((tmp_path / "figures.json").read_text(encoding="utf-8"))

    doc30 = export("--days", "30")
    assert doc30["coverage"] == {"sessions": 3, "read": 2, "stored": 1, "older": 0}
    doc45 = export("--days", "45")
    assert doc45["coverage"] == {"sessions": 4, "read": 2, "stored": 2, "older": 0}
    # The 365-day window holds the same 4 sessions: it differs from the 30-day export and covers the 40-day one.
    doc365 = export("--days", "365")
    assert doc365["compactions"]["sessions"] == 4 != doc30["compactions"]["sessions"]
    for doc in (doc45, doc365):
        doc.pop("window_days"), doc.pop("generated_on")
    assert doc45 == doc365
    assert "coverage" not in export("--project", "proj-b")
    (world[1] / "config.toml").write_text('exclude_projects = ["proj-a"]\n', encoding="utf-8")
    doc = export()
    assert "coverage" not in doc
    assert doc["compactions"]["sessions"] == 1


def test_a_window_whose_transcripts_are_all_gone_still_exports(tmp_path, capsys):
    world = store_with_gone_sessions(
        tmp_path, {"proj-a": {"keep": S("keep", 40), "gone-a": S("gone-a", 2)}}, {"gone-a"}
    )
    code, _stdout, _err = tuning_export(world, tmp_path / "figures.json", "--days", "30", capsys=capsys)
    assert code == 0
    doc = json.loads((tmp_path / "figures.json").read_text(encoding="utf-8"))
    assert doc["coverage"] == {"sessions": 1, "read": 0, "stored": 1, "older": 0}


def test_the_cli_report_says_it_covers_1_of_3_and_carries_the_counts(tmp_path, capsys):
    world = three_sessions(tmp_path)
    code, out, _err = report(world, capsys=capsys)
    assert code == 0
    lines = out.splitlines()
    assert "- " + ONE in lines
    assert lines.index("- " + ONE) < lines.index("## Assumptions")
    code, out, _err = report(world, "--json", capsys=capsys)
    meta = json.loads(out)["report"]["meta"]
    assert (meta["sessions"], meta["sessions_left_out"]) == (1, 2)
    assert "sessions_known" not in meta and "kept_days" not in meta
    page = tmp_path / "report.html"
    code, _out, _err = report(world, "--html", str(page), capsys=capsys)
    assert code == 0
    assert page.read_text(encoding="utf-8").count(html.escape(ONE)) == 1
    for flags in (("--window-by", "mtime"), ("--limit", "5")):
        code, out, _err = report(world, *flags, capsys=capsys)
        assert code == 0 and "These figures cover" not in out
    drop_store(world[1])
    code, out, _err = report(world, capsys=capsys)
    assert code == 0 and "These figures cover" not in out
    code, out, _err = report(world, "--json", capsys=capsys)
    meta = json.loads(out)["report"]["meta"]
    assert meta["sessions_left_out"] == 0


def test_export_says_on_stderr_what_it_covers_and_writes_the_same_file(tmp_path, capsys):
    projects, config_dir, _claude_root = three_sessions(tmp_path)
    out = tmp_path / "export.json"
    argv = [
        "export",
        "--projects-root",
        str(projects),
        "--config-dir",
        str(config_dir),
        "--all-projects",
        "--days",
        "30",
        "--format",
        "json",
        "--generated-at",
        "2026-11-12T00:00:00Z",
        "--out",
        str(out),
    ]
    capsys.readouterr()
    assert cli.main(argv) == 0
    assert f"claudeglass export: {ONE}" in capsys.readouterr().err
    first = out.read_bytes()
    drop_store(config_dir)
    assert cli.main(argv) == 0
    assert "These figures cover" not in capsys.readouterr().err
    assert out.read_bytes() == first


def damage_digest(config_dir, session_id: str, blob: bytes) -> None:
    conn = sqlite3.connect(config_dir / "service.db")
    try:
        conn.execute("UPDATE transcripts SET digest_blob = ? WHERE session_id = ?", (blob, session_id))
        conn.commit()
    finally:
        conn.close()


DAMAGED_DIGESTS = {
    "not zlib": b"garbage",
    "zlib of bad utf-8": zlib.compress(b"\xff\xfe\xfa"),
    "zlib of text that is not json": zlib.compress(b"not json"),
    "json of the wrong shape": zlib.compress(json.dumps({"turns": 5}).encode()),
}


@pytest.mark.parametrize("blob", DAMAGED_DIGESTS.values(), ids=DAMAGED_DIGESTS.keys())
def test_one_damaged_digest_leaves_out_only_its_session(tmp_path, capsys, blob):
    world = three_sessions(tmp_path)
    damage_digest(world[1], "gone-b", blob)
    out = tmp_path / "figures.json"
    code, _stdout, err = tuning_export(world, out, capsys=capsys)
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    # gone-a is still read from the store; gone-b is left out and is not counted as read from it.
    assert doc["coverage"] == {"sessions": 3, "read": 1, "stored": 1, "older": 0}
    assert doc["compactions"]["sessions"] == 2
    assert err == (
        "claudeglass tuning export: These figures cover 2 of 3 sessions in this window. "
        "The transcript of the other session was not found, and the dashboard still has that session.\n"
        "claudeglass tuning export: 1 of 3 sessions in this window is read from what the dashboard kept, "
        "because their transcripts were not found.\n"
    )
    # The report still counts the session the export could not read.
    _code, text, _err = report(world, capsys=capsys)
    assert "- " + ONE in text.splitlines()


GONE_0_OF_1 = (
    "These figures cover 0 of 1 session in this window. "
    "The transcript of the other session was not found, and the dashboard still has that session."
)


def run_cli(world, command, *extra, capsys):
    projects, config_dir, claude_root = world
    capsys.readouterr()
    code = cli.main(
        [*command, "--projects-root", str(projects), "--config-dir", str(config_dir), *extra]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def every_session_gone(tmp_path):
    """One session of the window in the store, its transcript gone; the only other one is outside the window."""
    return store_with_gone_sessions(
        tmp_path, {"proj-a": {"keep": S("keep", 40), "gone-a": S("gone-a", 2)}}, {"gone-a"}
    )


def project_folder_removed(tmp_path):
    """``proj-a`` holds a session; ``proj-old`` held one whose transcript and folder are gone."""
    world = store_with_gone_sessions(
        tmp_path, {"proj-a": {"keep": S("keep", 1)}, "proj-old": {"old-1": S("old-1", 2)}}, {"old-1"}
    )
    shutil.rmtree(world[0] / "proj-old")
    return world


@pytest.mark.parametrize("command", [["report"], ["export", "--format", "json"]])
def test_report_and_export_say_what_the_dashboard_has_when_no_session_is_found(tmp_path, capsys, command):
    world = every_session_gone(tmp_path)
    code, out, err = run_cli(world, command, "--all-projects", "--days", "30", capsys=capsys)
    assert code == 1 and out == ""
    lines = err.splitlines()
    assert lines[0].startswith(f"claudeglass {command[0]}: no sessions found under ")
    assert lines[1:] == [f"claudeglass {command[0]}: {GONE_0_OF_1}"]


@pytest.mark.parametrize("command", [["report"], ["export", "--format", "json"], ["tuning", "export"]])
def test_a_project_whose_folder_is_gone_still_gets_the_line(tmp_path, capsys, command):
    world = project_folder_removed(tmp_path)
    code, _out, err = run_cli(world, command, "--project", "proj-old", "--days", "30", capsys=capsys)
    assert code == 1
    name = "tuning export" if command[0] == "tuning" else command[0]
    lines = err.splitlines()
    assert lines[0].startswith(f"claudeglass {name}: no matching project directories under ")
    assert lines[1:] == [f"claudeglass {name}: {GONE_0_OF_1}"]
    # A project the store knows nothing of gets no line.
    code, _out, err = run_cli(world, command, "--project", "proj-none", "--days", "30", capsys=capsys)
    assert code == 1 and len(err.splitlines()) == 1


def test_a_project_family_counts_the_store_projects_whose_folder_is_gone(tmp_path, capsys):
    world = project_folder_removed(tmp_path)
    out = tmp_path / "figures.json"
    code, _stdout, err = tuning_export(world, out, "--project-family", "proj-.*", capsys=capsys)
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["coverage"] == {"sessions": 2, "read": 1, "stored": 1, "older": 0}
    code, text, _err = run_cli(world, ["report"], "--project-family", "proj-.*", "--days", "30", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(text)["report"]["meta"]["sessions_left_out"] == 1


def test_no_command_writes_to_the_store_not_even_into_its_wal(tmp_path, capsys):
    world = three_sessions(tmp_path)
    before = store_state(world[1])
    assert before[1].count("INSERT INTO") > 0
    for command, extra in (
        (["report"], ["--all-projects", "--days", "30"]),
        (["report"], ["--all-projects", "--days", "30", "--json"]),
        (["export", "--format", "json"], ["--all-projects", "--days", "30"]),
        (["tuning", "export"], []),
    ):
        code, _out, _err = run_cli(world, command, *extra, capsys=capsys)
        assert code == 0, command
        assert store_state(world[1]) == before, command


def test_project_names_match_the_store_slugs_whatever_their_case(tmp_path, capsys):
    """The store was fed from ``c--Dev-x``; the folder is now ``C--Dev-x``."""
    world = store_with_gone_sessions(
        tmp_path,
        {"c--Dev-x": {"keep": S("keep", 1), "gone-a": S("gone-a", 2), "gone-b": S("gone-b", 3)}},
        {"gone-a", "gone-b"},
    )
    projects = world[0]
    (projects / "c--Dev-x").rename(projects / "tmp-case")
    (projects / "tmp-case").rename(projects / "C--Dev-x")
    assert [p.name for p in projects.iterdir()] == ["C--Dev-x"]
    out = tmp_path / "figures.json"
    code, _stdout, _err = tuning_export(world, out, "--project", "C--Dev-x", capsys=capsys)
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["coverage"] == {"sessions": 3, "read": 1, "stored": 2, "older": 0}
    code, text, _err = run_cli(world, ["report"], "--project", "C--Dev-x", "--days", "30", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(text)["report"]["meta"]["sessions_left_out"] == 2

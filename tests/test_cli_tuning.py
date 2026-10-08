"""``claudeglass tuning export|summary`` (Phase 7's CLI, ``cli._cmd_tuning``):
the wiring around ``tuning.py``, which ``tests/test_tuning.py`` tests in full.

What is held here is the command's own promises. ``export`` loads the same
window ``export`` does (``--days``, 30 by default, 1 to 365), builds the
document, checks it and writes it whole or not at all: a build the checks
refuse prints its problem lines (a key path and the check, never a value) and
exits 2 with nothing written. ``summary`` runs the same checks on the file and
prints no figure from one that fails them.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import cli, parse, tuning

from helpers import assert_privacy_deep, turn_line, user_str_line, write_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tuning"
NOW = datetime.now(timezone.utc).replace(microsecond=0)

#: Strings that are in the sessions below and must not be in anything exported.
PRIVATE = ("acme-secret-project", "SecretCodename", "widget.py", "other-project")


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"d" * 32)


def _at(days_ago: float, seconds: int = 0) -> str:
    return (NOW - timedelta(days=days_ago) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _session(days_ago: float, entrypoint: str) -> list[dict]:
    """One short session: a message, an answer that ends on a tag, a follow-up."""
    return [
        user_str_line(
            "please fix the SecretCodename widget in C:\\Users\\paul\\clients\\acme\\widget.py",
            origin={"kind": "human"},
            timestamp=_at(days_ago),
            entrypoint=entrypoint,
        ),
        turn_line(
            content=[{"type": "text", "text": "Done.\n[cg: task=feature level=easy shift=new size=s]"}],
            timestamp=_at(days_ago, 20),
        ),
        user_str_line("now add a test for it", origin={"kind": "human"}, timestamp=_at(days_ago, 300)),
        turn_line(content=[{"type": "text", "text": "Added."}], timestamp=_at(days_ago, 320)),
    ]


@dataclass
class World:
    root: Path
    projects: Path
    config_dir: Path
    claude_root: Path
    out: Path


@pytest.fixture()
def world(tmp_path):
    projects = tmp_path / "projects"
    alpha = projects / "acme-secret-project"
    beta = projects / "other-project"
    alpha.mkdir(parents=True)
    beta.mkdir()
    write_jsonl(alpha / "s1.jsonl", _session(1, "cli"))
    # A session 100 days back: in a 365-day window, not in a 30-day one.
    write_jsonl(beta / "s2.jsonl", _session(100, "claude-desktop"))
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    return World(tmp_path, projects, tmp_path / "cg", claude_root, tmp_path / "figures.json")


def _export(world: World, *argv: str) -> int:
    return cli.main(
        [
            "tuning",
            "export",
            "--projects-root",
            str(world.projects),
            "--config-dir",
            str(world.config_dir),
            "--claude-root",
            str(world.claude_root),
            *argv,
        ]
    )


def _summary(*argv: str) -> int:
    return cli.main(["tuning", "summary", *argv])


def _stdout_doc(world: World, capsys, *argv: str) -> dict:
    assert _export(world, *argv) == 0
    out, err = capsys.readouterr()
    assert err == ""
    return json.loads(out)


def _sample() -> dict:
    return json.loads((FIXTURES / "sample.json").read_text(encoding="utf-8"))


def _write(path: Path, doc) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _leftovers(folder: Path) -> list[str]:
    """Anything in ``folder`` that is a temporary file of the writer."""
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


# -- wiring ----------------------------------------------------------------------------------


def test_tuning_is_a_subcommand_whose_output_is_data():
    assert "tuning" in cli.SUBCOMMANDS
    assert "tuning" in cli._DATA_OUTPUT
    assert cli.TUNING_ACTIONS == ("export", "summary")
    assert (cli.TUNING_DEFAULT_DAYS, cli.TUNING_MAX_DAYS) == (30, 365)


def test_help_lists_tuning_and_its_two_actions(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    listing = capsys.readouterr().out
    assert re.search(r"^\s+tuning\s+\S", listing, re.M)

    with pytest.raises(SystemExit) as raised:
        cli.main(["tuning", "--help"])
    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for word in ("export", "summary", "--out", "--days"):
        assert word in text


def test_an_action_other_than_export_or_summary_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["tuning", "import", "figures.json"])
    assert raised.value.code == 2
    assert capsys.readouterr().out == ""


# -- export ----------------------------------------------------------------------------------


def test_export_to_a_file_writes_a_document_that_validates(world, capsys):
    assert _export(world, "--out", str(world.out)) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert world.out.exists()

    doc = tuning.read_file(world.out)
    assert tuning.validate(doc) == []
    assert doc["kind"] == "claudeglass-tuning" and doc["format"] == 1
    assert doc["window_days"] == 30
    assert doc["generated_on"] in {NOW.date().isoformat(), NOW.astimezone().date().isoformat()}
    for block in ("capture", "prompting", "tags", "pieces", "agents", "overhead"):
        assert block in doc

    # One line says where it went and how to read it; the file stays whole.
    assert str(world.out) in out
    assert re.search(r"claudeglass tuning summary .*figures\.json", out)
    assert world.out.read_text(encoding="utf-8") == tuning.dumps(doc)
    assert b"\r" not in world.out.read_bytes()
    assert _leftovers(world.root) == []


def test_export_to_stdout_is_valid_json_and_nothing_else(world, capsys):
    assert _export(world) == 0
    out, err = capsys.readouterr()
    assert err == ""
    doc = tuning.loads(out)  # validated too
    assert json.loads(out) == doc
    assert doc["overhead"]["entrypoints"] == {"cli": 1}
    assert not world.out.exists()


def test_the_default_window_is_30_days_and_days_sets_it(world, capsys):
    assert _stdout_doc(world, capsys)["window_days"] == 30
    assert _stdout_doc(world, capsys, "--days", "7")["window_days"] == 7
    assert _stdout_doc(world, capsys, "--days", "2")["window_days"] == 2
    assert _stdout_doc(world, capsys, "--days", "365")["window_days"] == 365


def test_days_filters_the_sessions_the_way_export_does(world, capsys):
    short = _stdout_doc(world, capsys, "--days", "30")
    long = _stdout_doc(world, capsys, "--days", "365")
    assert short["overhead"]["entrypoints"] == {"cli": 1}
    assert long["overhead"]["entrypoints"] == {"claude-desktop": 1, "cli": 1}
    assert short["pieces"]["total"] <= long["pieces"]["total"]


@pytest.mark.parametrize("days", ["366", "400", "100000"])
def test_days_over_a_year_is_refused_and_writes_nothing(world, capsys, days):
    assert _export(world, "--days", days, "--out", str(world.out)) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "--days must be between 1 and 365" in err
    assert days in err
    assert not world.out.exists()
    assert _leftovers(world.root) == []


@pytest.mark.parametrize("days", ["0", "-5", "abc"])
def test_days_below_one_or_not_a_number_is_a_usage_error(world, capsys, days):
    with pytest.raises(SystemExit) as raised:
        _export(world, "--days", days, "--out", str(world.out))
    assert raised.value.code == 2
    assert capsys.readouterr().out == ""
    assert not world.out.exists()


@pytest.mark.parametrize("flag", [("--since", "2026-01-01"), ("--until", "2026-12-31"), ("--limit", "5")])
def test_a_window_other_than_days_is_refused(world, capsys, flag):
    assert _export(world, *flag, "--out", str(world.out)) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "--days only" in err
    assert not world.out.exists()


def test_export_takes_out_not_a_file_name(world, capsys):
    assert cli.main(["tuning", "export", str(world.out), "--projects-root", str(world.projects)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "takes --out PATH" in err
    assert not world.out.exists()


def test_export_includes_every_project_unless_one_is_named(world, monkeypatch):
    seen: list[list[str]] = []
    real = cli._load_corpus_for_args

    def spy(args, config, config_dir, project_dirs, **kw):
        seen.append(sorted(p.name for p in project_dirs))
        return real(args, config, config_dir, project_dirs, **kw)

    monkeypatch.setattr(cli, "_load_corpus_for_args", spy)
    assert _export(world, "--out", str(world.out)) == 0
    assert _export(world, "--project", "other-project", "--days", "365", "--out", str(world.out)) == 0
    assert seen == [["acme-secret-project", "other-project"], ["other-project"]]


def test_export_counts_the_dashboard_ratings_and_tip_card_answers(world, monkeypatch):
    seen = {}
    real = tuning.build

    def spy(*args, **kw):
        seen.update(ratings=kw.get("ratings"), tip_feedback=kw.get("tip_feedback"))
        return real(*args, **kw)

    marks = {"no-such-session": {"tip_hint": "plan_fresh", "tip": "useful"}}
    cards = {("tip", "drip_feed"): {"answer": "wrong", "set_at": "2026-09-01T00:00:00Z"}}
    monkeypatch.setattr(cli, "_merge_dashboard_marks", lambda config_dir, overrides: (overrides, marks))
    monkeypatch.setattr(cli, "_dashboard_tip_feedback", lambda config_dir: cards)
    monkeypatch.setattr(tuning, "build", spy)
    assert _export(world, "--out", str(world.out)) == 0
    assert seen == {"ratings": marks, "tip_feedback": cards}
    doc = json.loads(world.out.read_text(encoding="utf-8"))
    assert doc["capture"]["hints"]["drip_feed"]["wrong"] == 1


def test_export_holds_nothing_that_names_anything(world, capsys):
    assert _export(world, "--days", "365", "--out", str(world.out)) == 0
    capsys.readouterr()
    text = world.out.read_text(encoding="utf-8")
    assert_privacy_deep(json.loads(text))
    for name in (*PRIVATE, str(world.root), world.root.name, "Users", "paul", "acme", "@", "://"):
        assert name not in text, name


def test_export_with_no_matching_project_exits_1_and_writes_nothing(world, capsys):
    assert _export(world, "--project", "no-such-project", "--out", str(world.out)) == 1
    out, err = capsys.readouterr()
    assert out == "" and "no matching project directories" in err
    assert not world.out.exists()


def test_export_with_no_sessions_in_the_window_exits_1_and_writes_nothing(world, capsys):
    write_jsonl(world.projects / "acme-secret-project" / "s1.jsonl", _session(200, "cli"))
    assert _export(world, "--project", "acme-secret-project", "--days", "30", "--out", str(world.out)) == 1
    out, err = capsys.readouterr()
    assert out == "" and "no sessions found" in err
    assert not world.out.exists()


def test_a_bad_rate_card_is_exit_2(world, capsys, tmp_path):
    bad = tmp_path / "pricing.toml"
    bad.write_text("this is not toml = = =", encoding="utf-8")
    assert _export(world, "--pricing", str(bad), "--out", str(world.out)) == 2
    assert capsys.readouterr().out == ""
    assert not world.out.exists()


# -- export: a build the checks refuse ---------------------------------------------------------


def _leak_a_path_into(monkeypatch, block: str, key: str) -> None:
    """Make one block's builder write a Windows user-folder path where the spec wants a count."""
    original = tuning._BLOCKS[block]

    def leaky(ctx):
        built = original[1](ctx)
        built[key] = "C:\\Users\\paul\\clients\\acme"
        return built

    monkeypatch.setitem(tuning._BLOCKS, block, (original[0], leaky, original[2]))


def test_a_build_that_fails_validation_exits_2_and_leaves_no_file(world, capsys, monkeypatch):
    _leak_a_path_into(monkeypatch, "pieces", "total")
    assert _export(world, "--out", str(world.out)) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert not world.out.exists()
    assert _leftovers(world.root) == []
    # The lines name the key and the check, not what was found.
    assert "pieces.total: must be a whole number" in err
    assert "document: holds a drive path" in err
    for value in ("paul", "acme", "clients", "C:"):
        assert value not in err


def test_a_build_that_fails_validation_prints_no_json_to_stdout(world, capsys, monkeypatch):
    _leak_a_path_into(monkeypatch, "pieces", "total")
    assert _export(world) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "nothing was written" in err


def test_a_refused_export_leaves_an_existing_file_as_it_was(world, capsys, monkeypatch):
    world.out.write_text("the figures from last week\n", encoding="utf-8")
    _leak_a_path_into(monkeypatch, "pieces", "total")
    assert _export(world, "--out", str(world.out)) == 2
    capsys.readouterr()
    assert world.out.read_text(encoding="utf-8") == "the figures from last week\n"
    assert _leftovers(world.root) == []


def test_export_replaces_an_existing_file_whole(world, capsys):
    world.out.write_text("x" * 100_000, encoding="utf-8")
    assert _export(world, "--out", str(world.out)) == 0
    capsys.readouterr()
    assert tuning.validate(json.loads(world.out.read_text(encoding="utf-8"))) == []
    assert _leftovers(world.root) == []


def test_a_file_that_cannot_be_written_is_exit_2_with_no_partial_file(world, capsys, tmp_path):
    missing_folder = tmp_path / "nowhere" / "figures.json"
    assert _export(world, "--out", str(missing_folder)) == 2
    out, err = capsys.readouterr()
    assert out == "" and "cannot write" in err
    assert not missing_folder.parent.exists()

    a_folder = tmp_path / "a-folder"
    a_folder.mkdir()
    assert _export(world, "--out", str(a_folder)) == 2
    assert "cannot write" in capsys.readouterr().err
    assert a_folder.is_dir() and list(a_folder.iterdir()) == []
    assert _leftovers(tmp_path) == []


def test_the_read_back_command_follows_this_installs_form(world, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDEGLASS_COMMAND", "py -m claudeglass")
    assert _export(world, "--out", str(world.out)) == 0
    out = capsys.readouterr().out
    assert "py -m claudeglass tuning summary " in out
    assert "\nclaudeglass tuning" not in out and not out.startswith("claudeglass tuning")


# -- summary ---------------------------------------------------------------------------------


def test_summary_prints_the_block_lines_of_an_exported_file(world, capsys):
    assert _export(world, "--out", str(world.out)) == 0
    capsys.readouterr()
    assert _summary(str(world.out)) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out == tuning.summary_text(tuning.read_file(world.out)) + "\n"
    lines = out.splitlines()
    assert lines[0].startswith("Tuning figures made on ") and "for the last 30 days" in lines[0]
    for start in ("Capture:", "Prompting:", "Pieces of work:", "Agent runs:", "Capture cost "):
        assert any(line.startswith(start) for line in lines), start
    assert "Sessions started from: cli 1." in lines


def test_summary_of_the_sample_document(capsys):
    assert _summary(str(FIXTURES / "sample.json")) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "Pieces of work: 67." in out
    assert "Of the 55 delivered pieces with a clear start, 10 needed changes after delivery." in out
    assert "Cold returns: 6, rewriting 900,000 cache tokens." in out
    assert out == tuning.summary_text(_sample()) + "\n"


def test_summary_refuses_a_file_with_a_windows_user_folder_path(tmp_path, capsys):
    doc = _sample()
    doc["capture"]["level"] = "C:\\Users\\paul\\clients\\acme"
    path = _write(tmp_path / "tampered.json", doc)
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == ""  # no figure at all
    assert "capture.level" in err and "document: holds a drive path" in err
    for value in ("paul", "acme", "clients", "C:"):
        assert value not in err


def test_summary_refuses_a_path_hidden_in_a_key_or_an_extra_block(tmp_path, capsys):
    doc = _sample()
    doc["tags"]["notes"] = {"C:\\Users\\paul\\x": 1}
    doc["secrets"] = "paul@example.com"
    path = _write(tmp_path / "tampered.json", doc)
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "document: holds an at sign" in err
    for value in ("paul", "example", "secrets", "notes"):
        assert value not in err


def test_summary_refuses_a_file_that_gives_a_key_twice(tmp_path, capsys):
    bs = chr(92)
    text = tuning.dumps(_sample()).replace(
        '"kind": "claudeglass-tuning"',
        '"kind": "C:' + bs * 2 + "Users" + bs * 2 + 'paul", "kind": "claudeglass-tuning"',
        1,
    )
    path = tmp_path / "twice.json"
    path.write_text(text, encoding="utf-8")
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == "" and "file: has a key more than once" in err
    assert "paul" not in err and "Users" not in err


def test_summary_refuses_a_file_exported_and_then_edited(world, capsys):
    assert _export(world, "--out", str(world.out)) == 0
    capsys.readouterr()
    doc = json.loads(world.out.read_text(encoding="utf-8"))
    doc["pieces"]["total"] = -1
    _write(world.out, doc)
    assert _summary(str(world.out)) == 2
    out, err = capsys.readouterr()
    assert out == "" and "pieces.total" in err


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("this is not json", "file: is not JSON"),
        ("[1, 2, 3]", "document: must be an object"),
        ("{}", "kind: is missing"),
        ('{"kind": "something-else"}', "kind"),
        ("", "file: is not JSON"),
    ],
)
def test_summary_refuses_a_file_that_is_not_a_tuning_document(tmp_path, capsys, text, what):
    path = tmp_path / "other.json"
    path.write_text(text, encoding="utf-8")
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == "" and what in err


def test_summary_refuses_a_file_over_the_size_limit_without_reading_it(tmp_path, capsys, monkeypatch):
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (2 * tuning.MAX_BYTES + 1))
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("read a file over the limit"))
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == "" and "file: is over the size limit" in err


def test_summary_refuses_a_document_over_the_size_limit(tmp_path, capsys):
    doc = _sample()
    doc["padding"] = "x" * tuning.MAX_BYTES
    path = _write(tmp_path / "big.json", doc)
    assert _summary(str(path)) == 2
    out, err = capsys.readouterr()
    assert out == "" and "over the size limit" in err


def test_summary_of_a_file_that_is_not_there_or_not_text_is_exit_2(tmp_path, capsys):
    assert _summary(str(tmp_path / "nothing.json")) == 2
    out, err = capsys.readouterr()
    assert out == "" and "cannot be read" in err

    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    assert _summary(str(binary)) == 2
    assert capsys.readouterr().out == ""

    assert _summary(str(tmp_path)) == 2  # a folder
    assert capsys.readouterr().out == ""


def test_summary_needs_a_file(capsys):
    assert _summary() == 2
    out, err = capsys.readouterr()
    assert out == "" and "needs the file to read" in err


@pytest.mark.parametrize("flag", [("--out", "x.json"), ("--days", "7"), ("--since", "2026-01-01"), ("--until", "2026-02-01")])
def test_summary_has_no_window_or_output_of_its_own(capsys, flag):
    assert _summary(str(FIXTURES / "sample.json"), *flag) == 2
    out, err = capsys.readouterr()
    assert out == "" and "does not apply" in err


def test_summary_prints_the_first_problems_and_counts_the_rest(tmp_path, capsys):
    doc = _sample()
    for i in range(40):
        doc[f"extra{i}"] = 1
    assert _summary(str(_write(tmp_path / "many.json", doc))) == 2
    out, err = capsys.readouterr()
    lines = [line for line in err.splitlines() if line.startswith("  ")]
    assert out == ""
    assert len(lines) == cli._TUNING_PROBLEMS_SHOWN + 1
    assert re.fullmatch(r"  and \d+ more", lines[-1])
    assert "extra" not in err


def test_summary_needs_no_sessions_config_or_projects(tmp_path, capsys):
    """It reads the file alone: a machine with no Claude Code projects can read one."""
    assert cli.main(["tuning", "summary", str(FIXTURES / "sample.json"), "--projects-root", str(tmp_path / "none")]) == 0
    assert capsys.readouterr().out.startswith("Tuning figures made on ")


# -- the round trip --------------------------------------------------------------------------


def test_what_export_writes_summary_reads_and_a_copy_of_it_still_validates(world, capsys, tmp_path):
    assert _export(world, "--days", "365", "--out", str(world.out)) == 0
    capsys.readouterr()
    copied = tmp_path / "other-machine" / "copy.json"
    copied.parent.mkdir()
    copied.write_bytes(world.out.read_bytes())
    assert _summary(str(copied)) == 0
    first = capsys.readouterr().out
    assert _summary(str(world.out)) == 0
    assert capsys.readouterr().out == first
    assert tuning.validate(copy.deepcopy(json.loads(copied.read_text(encoding="utf-8")))) == []

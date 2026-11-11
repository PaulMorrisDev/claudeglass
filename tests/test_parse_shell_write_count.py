"""``Turn.shell_write_count`` (``parse``): how many write targets a
turn's shell commands named outside the temp dir, repeats kept. A count
only, taken without a salt, and taken back for a command that never ran."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from claudeglass import parse
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

REPO = "C:/Dev/repo"
TEMP = tempfile.gettempdir()
TEMP_SLASHED = TEMP.replace("\\", "/")


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _turn(tmp_path, blocks, results, cwd=REPO):
    path = tmp_path / "t.jsonl"
    reply = turn_line(content=list(blocks), timestamp=_ts(1), cwd=cwd)
    reply["message"]["stop_reason"] = "tool_use"
    final = turn_line(content=[{"type": "text", "text": "done"}], timestamp=_ts(3))
    final["message"]["stop_reason"] = "end_turn"
    write_jsonl(path, [
        user_str_line("go", timestamp=_ts(0)),
        reply,
        user_block_line(list(results), timestamp=_ts(2)),
        final,
    ])
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    return next(t for t in result.turns if t.tool_use_ids)


def _bash(command, tool_use_id="t1"):
    return tool_use_block("Bash", tool_use_id, {"command": command})


def _ok(tool_use_id="t1"):
    return tool_result_block(tool_use_id, "ok")


@pytest.fixture(params=["no salt", "salt"])
def salt(request, monkeypatch):
    """Every case runs both ways: the count must not depend on the salt."""
    monkeypatch.setattr(parse, "_SALT", b"s" * 32 if request.param == "salt" else None)
    return request.param


def test_a_bash_write_to_a_repo_file_counts(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > src/a.py")], [_ok()])
    assert turn.shell_write_count == 1
    # The salted hashes still come only with a salt.
    assert len(turn.edit_target_hashes) == (1 if salt == "salt" else 0)


def test_a_powershell_write_to_a_repo_file_counts(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("PowerShell", "t1", {"command": "Set-Content -Path src\\a.ts -Value 'x'"}),
    ], [_ok()], cwd="C:\\Dev\\repo")
    assert turn.shell_write_count == 1


def test_every_target_a_command_names_counts(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("sed -i 's/a/b/' src/a.py src/b.py"), _bash("echo x > c.txt", "t2")],
                 [_ok("t1"), _ok("t2")])
    assert turn.shell_write_count == 3


def test_a_file_written_twice_counts_twice(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > a.txt && echo y >> a.txt")], [_ok()])
    assert turn.shell_write_count == 2


@pytest.mark.parametrize(
    "target",
    [TEMP_SLASHED + "/scratch.txt", TEMP + "\\scratch.txt", TEMP_SLASHED + "/sub/x.txt"],
    ids=["forward slashes", "backslashes", "a folder in it"],
)
def test_a_write_to_the_temp_dir_is_not_counted(tmp_path, salt, target):
    turn = _turn(tmp_path, [_bash(f'echo x > "{target}"')], [_ok()])
    assert turn.shell_write_count == 0


def test_a_write_to_the_git_bash_form_of_the_temp_dir_is_not_counted(tmp_path, salt):
    drive = re.match(r"^([A-Za-z]):/", TEMP_SLASHED)
    if drive is None:
        pytest.skip("the temp dir has no drive letter")
    msys = f"/{drive.group(1).lower()}/{TEMP_SLASHED[3:]}/scratch.txt"
    turn = _turn(tmp_path, [_bash(f'echo x > "{msys}"')], [_ok()])
    assert turn.shell_write_count == 0


def test_a_write_to_slash_tmp_is_not_counted(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > /tmp/x.txt")], [_ok()])
    assert turn.shell_write_count == 0


def test_a_powershell_write_to_the_temp_dir_is_not_counted(tmp_path, salt):
    command = f"[IO.File]::WriteAllText('{TEMP}\\x.json', $json)"
    turn = _turn(tmp_path, [tool_use_block("PowerShell", "t1", {"command": command})], [_ok()])
    assert turn.shell_write_count == 0


def test_a_path_that_climbs_out_of_the_temp_dir_counts(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > /tmp/../repo/a.py")], [_ok()])
    assert turn.shell_write_count == 1


def test_a_directory_that_only_starts_like_the_temp_dir_counts(tmp_path, salt):
    turn = _turn(tmp_path, [_bash(f'echo x > "{TEMP_SLASHED}2/a.py"'), _bash("echo x > /tmpfiles/b.py", "t2")],
                 [_ok("t1"), _ok("t2")])
    assert turn.shell_write_count == 2


def test_only_the_write_outside_the_temp_dir_is_counted(tmp_path, salt):
    turn = _turn(tmp_path, [_bash(f'echo x > /tmp/x.txt && echo y > "{TEMP_SLASHED}/y.txt" && echo z > z.txt')],
                 [_ok()])
    assert turn.shell_write_count == 1


def test_a_command_that_writes_nothing_counts_nothing(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("npm test > test.log"), _bash("git status", "t2")], [_ok("t1"), _ok("t2")])
    assert turn.shell_write_count == 0


def test_an_edit_tool_call_is_not_a_shell_write(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/a.py"}),
        tool_use_block("Write", "t2", {"file_path": "C:/Dev/repo/b.py"}),
    ], [_ok("t1"), _ok("t2")])
    assert turn.shell_write_count == 0


@pytest.mark.parametrize("message", [
    "The user doesn't want to proceed with this tool use. The tool use was rejected.",
    "PreToolUse:Bash hook error: [guard.sh]: not here",
])
def test_a_command_that_was_denied_or_blocked_is_taken_back(tmp_path, salt, message):
    turn = _turn(tmp_path, [_bash("echo x > src/a.py")], [tool_result_block("t1", message, is_error=True)])
    assert turn.shell_write_count == 0


def test_a_command_that_ran_and_failed_still_counts(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > src/a.py && false")],
                 [tool_result_block("t1", "Exit code 1", is_error=True)])
    assert turn.shell_write_count == 1


def test_only_the_command_that_never_ran_is_taken_back(tmp_path, salt):
    turn = _turn(tmp_path, [
        _bash("echo a > a.txt && echo b > b.txt", "t1"),
        _bash("echo c > c.txt", "t2"),
        _bash("echo d > d.txt", "t3"),
    ], [
        tool_result_block("t1", "The user doesn't want to proceed with this tool use.", is_error=True),
        tool_result_block("t2", "Exit code 1", is_error=True),
        _ok("t3"),
    ])
    assert turn.shell_write_count == 2


# -- the part of it inside a .claude folder (``Turn.config_edit_count``) ----------------


def test_a_shell_write_into_a_claude_folder_counts_as_config_as_well(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo a > /home/me/.claude/a.md && echo b > src/b.py")], [_ok()])
    assert (turn.shell_write_count, turn.config_edit_count) == (2, 1)


def test_an_edit_call_aimed_at_a_claude_folder_counts_as_config(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Users/me/.claude/plans/p.md"}),
        tool_use_block("Write", "t2", {"file_path": "C:\\Users\\me\\.claude\\memory\\m.md"}),
        tool_use_block("Edit", "t3", {"file_path": "C:/Dev/repo/a.py"}),
    ], [_ok("t1"), _ok("t2"), _ok("t3")])
    assert turn.config_edit_count == 2 and turn.shell_write_count == 0


def test_a_write_to_the_temp_dir_is_neither_a_shell_write_nor_config(tmp_path, salt):
    turn = _turn(tmp_path, [_bash(f'echo x > "{TEMP_SLASHED}/.claude/x.txt"')], [_ok()])
    assert (turn.shell_write_count, turn.config_edit_count) == (0, 0)


def test_config_edits_are_taken_back_with_the_call_that_never_ran_or_failed(tmp_path, salt):
    turn = _turn(tmp_path, [
        _bash("echo a > /home/me/.claude/a.md", "t1"),
        _bash("echo b > /home/me/.claude/b.md", "t2"),
        tool_use_block("Edit", "t3", {"file_path": "/home/me/.claude/c.md"}),
        tool_use_block("Edit", "t4", {"file_path": "/home/me/.claude/d.md"}),
    ], [
        tool_result_block("t1", "The user doesn't want to proceed with this tool use.", is_error=True),
        tool_result_block("t2", "Exit code 1", is_error=True),
        tool_result_block("t3", "File has not been read yet.", is_error=True),
        _ok("t4"),
    ])
    # The command that ran and failed still wrote; the edit that failed never did.
    assert turn.config_edit_count == 2 and turn.shell_write_count == 1


# -- the files a subagent changed (``Turn.agent_edit_files``) ---------------------------


def _agent_turn(tmp_path, result_fields, *, is_error=False):
    path = tmp_path / "agent.jsonl"
    reply = turn_line(content=[tool_use_block("Agent", "t1", {"prompt": "go"})], timestamp=_ts(1), cwd=REPO)
    reply["message"]["stop_reason"] = "tool_use"
    final = turn_line(content=[{"type": "text", "text": "done"}], timestamp=_ts(3))
    final["message"]["stop_reason"] = "end_turn"
    write_jsonl(path, [
        user_str_line("go", timestamp=_ts(0)),
        reply,
        user_block_line([tool_result_block("t1", "no" if is_error else "ok", is_error=is_error)], timestamp=_ts(2),
                        toolUseResult=result_fields),
        final,
    ])
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    return next(t for t in result.turns if t.tool_use_ids)


def test_the_files_a_subagent_changed_are_counted_from_its_result(tmp_path):
    assert _agent_turn(tmp_path, {"toolStats": {"editFileCount": 3, "readCount": 9}}).agent_edit_files == 3
    # Nothing changed, no stats, a count that isn't one, or a subagent that failed.
    assert _agent_turn(tmp_path, {"toolStats": {"editFileCount": 0}}).agent_edit_files == 0
    assert _agent_turn(tmp_path, {}).agent_edit_files == 0
    assert _agent_turn(tmp_path, {"toolStats": {"editFileCount": "3"}}).agent_edit_files == 0
    assert _agent_turn(tmp_path, {"toolStats": {"editFileCount": 3}}, is_error=True).agent_edit_files == 0


# -- what a reply changed (``edit_call_count``, ``edit_doc_count``, ``shell_change_count``) ---


def test_every_edit_call_to_your_work_counts_and_the_ones_to_documentation_count_again(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/a.py"}),
        tool_use_block("Write", "t2", {"file_path": "C:/Dev/repo/README.MD"}),
        tool_use_block("MultiEdit", "t3", {"file_path": "C:/Dev/repo/a.py"}),
        tool_use_block("NotebookEdit", "t4", {"notebook_path": "C:/Dev/repo/n.ipynb"}),
        tool_use_block("Edit", "t5", {"file_path": "C:/Dev/repo/notes.txt"}),
    ], [_ok(f"t{n}") for n in range(1, 6)])
    assert (turn.edit_call_count, turn.edit_doc_count) == (5, 2)


def test_an_edit_inside_a_claude_folder_or_with_no_path_is_not_a_change_of_yours(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Users/me/.claude/plans/p.md"}),
        tool_use_block("Edit", "t2", {"file_path": ""}),
        tool_use_block("Edit", "t3", {}),
        tool_use_block("Read", "t4", {"file_path": "C:/Dev/repo/a.py"}),
    ], [_ok(f"t{n}") for n in range(1, 5)])
    assert (turn.edit_call_count, turn.edit_doc_count) == (0, 0)


def test_an_edit_that_failed_or_was_denied_is_taken_back(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/a.py"}),
        tool_use_block("Edit", "t2", {"file_path": "C:/Dev/repo/b.md"}),
        tool_use_block("Edit", "t3", {"file_path": "C:/Dev/repo/c.md"}),
        tool_use_block("Edit", "t4", {"file_path": "C:/Dev/repo/d.py"}),
    ], [
        tool_result_block("t1", "File has not been read yet.", is_error=True),
        tool_result_block("t2", "The user doesn't want to proceed with this tool use.", is_error=True),
        _ok("t3"),
        _ok("t4"),
    ])
    assert (turn.edit_call_count, turn.edit_doc_count) == (2, 1)


@pytest.mark.parametrize("command", [
    "git merge main",
    "git pull --rebase",
    "git restore src/a.py",
    "mv a.py b.py",
    "rm -rf build",
    "mkdir -p out",
    "FOO=1 timeout 60 git pull",
    "npm test && sudo rm old.log",
    "patch -p1 < fix.diff",
])
def test_a_command_that_moves_or_removes_files_counts_once(tmp_path, salt, command):
    assert _turn(tmp_path, [_bash(command)], [_ok()]).shell_change_count == 1


@pytest.mark.parametrize("command", [
    "git status",
    "git commit -m 'merge it' && git push",
    "git log --merge",
    "npm test 2>&1",
    "pytest -q > /dev/null",
    "cat a.py | grep rm",
    "echo 'rm -rf build'",
    "ls",
])
def test_a_command_that_changes_no_file_counts_nothing(tmp_path, salt, command):
    assert _turn(tmp_path, [_bash(command)], [_ok()]).shell_change_count == 0


def test_a_powershell_command_that_removes_a_file_counts(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("PowerShell", "t1", {"command": "Remove-Item -Recurse build"}),
        tool_use_block("PowerShell", "t2", {"command": "Get-ChildItem build"}),
    ], [_ok("t1"), _ok("t2")], cwd="C:\\Dev\\repo")
    assert turn.shell_change_count == 1


def test_a_file_changing_command_is_counted_once_per_command_and_taken_back_only_when_it_never_ran(
    tmp_path, salt
):
    turn = _turn(tmp_path, [
        _bash("git merge a && git merge b", "t1"),
        _bash("git merge c", "t2"),
        _bash("git merge d", "t3"),
        _bash("git merge e", "t4"),
    ], [
        tool_result_block("t1", "The user doesn't want to proceed with this tool use.", is_error=True),
        tool_result_block("t2", "PreToolUse:Bash hook error: [guard.sh]: not here", is_error=True),
        tool_result_block("t3", "Exit code 1", is_error=True),
        _ok("t4"),
    ])
    # The merge that ran and failed may have moved files; the two that never ran moved none.
    assert turn.shell_change_count == 2


def test_the_counts_are_plain_numbers_and_do_not_depend_on_the_salt(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/a.py"}),
        _bash("git mv a.py b.py", "t2"),
    ], [_ok("t1"), _ok("t2")])
    assert (turn.edit_call_count, turn.edit_doc_count, turn.shell_change_count) == (1, 0, 1)
    assert all(type(n) is int for n in (turn.edit_call_count, turn.edit_doc_count, turn.shell_change_count))


# -- the part of it aimed at code (``Turn.shell_code_write_count``) --------------------


@pytest.mark.parametrize("command", [
    "echo x > src/a.py",
    "sed -i 's/a/b/' src/a.py",
    "echo x > notes.json",
    "echo x > app/reports/views.py",
    "echo x > docs/conf.py",
])
def test_a_shell_write_to_code_counts_as_a_code_write(tmp_path, salt, command):
    turn = _turn(tmp_path, [_bash(command)], [_ok()])
    assert turn.shell_code_write_count == 1
    assert turn.shell_write_count == 1


@pytest.mark.parametrize("command", [
    "cat > review-1.md <<'EOF'\nfindings\nEOF",
    "echo x >> notes.txt",
    "echo '{}' > audit-report.json",
    "echo '{}' > ledger.jsonl",
    "echo x > reports/out.csv",
    "echo x > docs/a.html",
    "echo x > /home/me/scratchpad/run.py",
    # A log, a table of results or a line-per-record ledger is a document wherever it sits.
    "echo x >> progress.log",
    "echo x >> verify.csv",
    "printf 'a,b' >> results.tsv",
    "echo '{}' >> progress.jsonl",
    "echo x >> src/run.log",
])
def test_a_shell_write_to_a_document_is_not_a_code_write(tmp_path, salt, command):
    turn = _turn(tmp_path, [_bash(command)], [_ok()])
    assert turn.shell_code_write_count == 0
    assert turn.shell_write_count == 1


def test_a_shell_write_to_temp_is_not_a_code_write(tmp_path, salt):
    turn = _turn(tmp_path, [_bash(f"echo x > {TEMP_SLASHED}/a.py")], [_ok()])
    assert turn.shell_code_write_count == 0


def test_only_code_targets_are_counted_when_a_command_writes_both(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo a > review.md && echo b > src/b.py")], [_ok()])
    assert turn.shell_write_count == 2
    assert turn.shell_code_write_count == 1


def test_a_code_write_that_never_ran_is_taken_back(tmp_path, salt):
    turn = _turn(tmp_path, [_bash("echo x > src/a.py")],
                 [tool_result_block("t1", "The user doesn't want to proceed with this tool use.", is_error=True)])
    assert turn.shell_code_write_count == 0


# -- where the work sits: only the folders below the working folder name a document ------

#: A project whose own path holds a folder with a document name.
_UNDER_A_DOCUMENT_FOLDER = [
    "/home/ana/docs/shop",
    "/srv/plans/api",
    "C:/Users/ana/reports/etl",
    "C:\\Users\\Ana\\Reports\\etl",
    "/home/ana/scratchpad/shop",
]


def _inside(cwd: str, rest: str) -> str:
    return cwd.replace("\\", "/") + "/" + rest


@pytest.mark.parametrize("cwd", _UNDER_A_DOCUMENT_FOLDER)
@pytest.mark.parametrize("command", [
    "echo '{}' > package.json",
    "echo '<p>' > web/index.html",
    "echo x > src/app.py",
])
def test_a_project_under_a_folder_named_like_a_document_folder_still_has_its_code_writes(tmp_path, salt, cwd, command):
    turn = _turn(tmp_path, [_bash(command)], [_ok()], cwd=cwd)
    assert turn.shell_write_count == 1 and turn.shell_code_write_count == 1


@pytest.mark.parametrize("cwd", _UNDER_A_DOCUMENT_FOLDER)
def test_an_edit_in_a_project_under_a_folder_named_like_a_document_folder_is_a_code_edit(tmp_path, salt, cwd):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": _inside(cwd, "package.json")}),
        tool_use_block("Write", "t2", {"file_path": _inside(cwd, "web/index.html")}),
    ], [_ok("t1"), _ok("t2")], cwd=cwd)
    assert turn.edit_code_count == 2


@pytest.mark.parametrize("cwd", _UNDER_A_DOCUMENT_FOLDER)
@pytest.mark.parametrize("command", [
    "echo '{}' > reports/summary.json",
    "echo x > docs/a.html",
    "echo '{}' > plans/2026/q3/data.json",
    "echo x > scratchpad/run.py",
    "echo x > docs/api/notes.md",
])
def test_the_projects_own_document_folders_still_hold_documents_wherever_it_sits(tmp_path, salt, cwd, command):
    turn = _turn(tmp_path, [_bash(command)], [_ok()], cwd=cwd)
    assert turn.shell_write_count == 1 and turn.shell_code_write_count == 0


def test_an_edit_into_the_projects_own_reports_folder_is_not_a_code_edit(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Write", "t1", {"file_path": "/home/ana/docs/shop/reports/out.json"}),
    ], [_ok("t1")], cwd="/home/ana/docs/shop")
    assert turn.edit_code_count == 0


@pytest.mark.parametrize(("target", "cwd", "code"), [
    # Inside the working folder: the folders below it decide.
    ("/home/ana/docs/shop/package.json", "/home/ana/docs/shop", True),
    ("/home/ana/docs/shop/docs/package.json", "/home/ana/docs/shop", False),
    ("/home/ana/docs/shop/docs/api/spec.json", "/home/ana/docs/shop", False),
    ("/home/ana/docs/shop/src/docs/spec.json", "/home/ana/docs/shop", False),
    ("/home/ana/docs/shop/src/docs.json", "/home/ana/docs/shop", True),
    # Case and slash direction do not tell two spellings of one folder apart.
    ("C:/Users/ana/reports/etl/package.json", "c:\\users\\ANA\\Reports\\etl", True),
    ("c:/users/ana/reports/etl/Reports/a.json", "C:\\Users\\ana\\reports\\etl\\", False),
    ("/c/Users/ana/reports/etl/package.json", "C:/Users/ana/reports/etl", True),
    ("/home/ana/docs/shop/package.json", "/home/ana/docs/shop/", True),
    # A relative target is below the working folder by being relative.
    ("docs/a.json", "/home/ana/docs/shop", False),
    ("package.json", "/home/ana/docs/shop", True),
    ("docs/a.json", None, False),
    ("package.json", None, True),
    # Outside the working folder, or with none: the folder the file sits in decides, nothing above it.
    ("/srv/docs/notes.json", "/home/ana/work", False),
    ("/srv/docs/shop/package.json", "/home/ana/work", True),
    ("/srv/docs/notes.json", None, False),
    ("/srv/docs/shop/package.json", None, True),
    ("/srv/plans/api/app.py", None, True),
    ("/srv/scratchpad/a.py", None, False),
    ("/srv/scratchpad/job/a.py", None, True),
    # A working folder that is not absolute names nothing: it is as good as none.
    ("/srv/docs/shop/package.json", "shop", True),
    ("/home/ana/work2/package.json", "/home/ana/work", True),
    # The folder rule is for .json, .html and .htm: the other endings need none.
    ("/home/ana/work/run.log", "/home/ana/work", False),
    ("/home/ana/work/data.csv", "/home/ana/work", False),
    ("/home/ana/work/data.tsv", "/home/ana/work", False),
    ("/home/ana/work/progress.jsonl", "/home/ana/work", False),
    ("/home/ana/work/package.json", "/home/ana/work", True),
    ("/home/ana/work/index.html", "/home/ana/work", True),
])
def test_the_folders_that_make_a_json_or_html_file_a_document_are_those_below_the_working_folder(target, cwd, code):
    assert parse._is_code_write_target(target, parse._temp_prefixes(), cwd) is code


def test_a_fresh_parse_measures_every_turn_the_synthetic_ones_included():
    # None is for a digest an older parser wrote. A turn this parser made, such as the one a compaction
    # summary adds, wrote no code: 0.
    path = Path(__file__).parent / "fixtures" / "real" / "session-a" / "55176945d92374c103c39f0aa42a1fffc37c.jsonl"
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert any(turn.is_synthetic for turn in result.turns)
    assert all(type(turn.shell_code_write_count) is int and type(turn.edit_code_count) is int for turn in result.turns)


# -- the edit-tool side of it (``Turn.edit_code_count``) ---------------------------------


def test_an_edit_to_code_counts_as_a_code_edit_even_inside_a_worktree_under_claude(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/.claude/worktrees/w/src/a.py"}),
        tool_use_block("Edit", "t2", {"file_path": "C:/Dev/repo/src/b.py"}),
    ], [_ok("t1"), _ok("t2")])
    assert turn.edit_code_count == 2


def test_an_edit_to_a_document_or_a_config_file_or_temp_is_not_a_code_edit(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Write", "t1", {"file_path": "~/.claude/agents/x.md"}),
        tool_use_block("Write", "t2", {"file_path": "C:/Dev/repo/REVIEW.md"}),
        tool_use_block("Edit", "t3", {"file_path": TEMP_SLASHED + "/x.py"}),
    ], [_ok("t1"), _ok("t2"), _ok("t3")])
    assert turn.edit_code_count == 0


def test_a_code_edit_that_failed_or_was_denied_is_taken_back(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/repo/a.py"}),
        tool_use_block("Edit", "t2", {"file_path": "C:/Dev/repo/b.py"}),
        tool_use_block("Edit", "t3", {"file_path": "C:/Dev/repo/c.py"}),
    ], [
        tool_result_block("t1", "File has not been read yet.", is_error=True),
        tool_result_block("t2", "The user doesn't want to proceed with this tool use.", is_error=True),
        _ok("t3"),
    ])
    assert turn.edit_code_count == 1

"""``Turn.shell_write_count`` (``parse``): how many write targets a
turn's shell commands named outside the temp dir, repeats kept. A count
only, taken without a salt, and taken back for a command that never ran."""

from __future__ import annotations

import re
import tempfile

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

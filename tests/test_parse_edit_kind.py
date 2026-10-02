"""``Turn.edit_kind`` (``parse``): "scratch" for an Edit/Write/MultiEdit/
NotebookEdit into the temp dir, "real" for one anywhere else, None for a
turn with no edit call. The temp dir is tested the way a shell write target
is, so the forward-slash, mixed-case and Git Bash forms of it match: Claude
Code often passes ``C:/Users/.../Temp/...`` where ``tempfile`` says
``C:\\Users\\...\\Temp``."""

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


def _edit(tool, target, tool_use_id="t1"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return tool_use_block(tool, tool_use_id, {key: target})


def _ok(tool_use_id="t1"):
    return tool_result_block(tool_use_id, "ok")


def _msys(path_slashed):
    drive = re.match(r"^([A-Za-z]):/", path_slashed)
    if drive is None:
        pytest.skip("the temp dir has no drive letter")
    return f"/{drive.group(1).lower()}/{path_slashed[3:]}"


@pytest.fixture(params=["no salt", "salt"])
def salt(request, monkeypatch):
    """Every case runs both ways: the kind must not depend on the salt."""
    monkeypatch.setattr(parse, "_SALT", b"s" * 32 if request.param == "salt" else None)
    return request.param


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
@pytest.mark.parametrize(
    "target",
    [
        TEMP + "\\scratch.py",
        TEMP_SLASHED + "/scratch.py",
        TEMP_SLASHED + "/sub/dir/scratch.py",
        TEMP.upper() + "\\Scratch.py",
        TEMP_SLASHED.swapcase() + "/Scratch.py",
        "/tmp/scratch.py",
    ],
    ids=["backslashes", "forward slashes", "a folder in it", "upper case", "mixed case", "slash tmp"],
)
def test_an_edit_into_the_temp_dir_is_scratch(tmp_path, salt, tool, target):
    turn = _turn(tmp_path, [_edit(tool, target)], [_ok()])
    assert turn.edit_kind == "scratch"


def test_an_edit_into_the_git_bash_form_of_the_temp_dir_is_scratch(tmp_path, salt):
    turn = _turn(tmp_path, [_edit("Write", _msys(TEMP_SLASHED) + "/scratch.py")], [_ok()])
    assert turn.edit_kind == "scratch"


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
@pytest.mark.parametrize(
    "target",
    ["C:/Dev/repo/src/a.py", "C:\\Dev\\repo\\src\\a.py", "src/a.py", "/home/me/repo/a.py"],
    ids=["forward slashes", "backslashes", "relative", "posix"],
)
def test_an_edit_to_a_repo_file_is_real(tmp_path, salt, tool, target):
    turn = _turn(tmp_path, [_edit(tool, target)], [_ok()])
    assert turn.edit_kind == "real"


def test_a_directory_that_only_starts_like_the_temp_dir_is_real(tmp_path, salt):
    for target in (f"{TEMP_SLASHED}2/x.py", f"{TEMP}2\\x.py", "/tmpfiles/x.py"):
        turn = _turn(tmp_path, [_edit("Edit", target)], [_ok()])
        assert turn.edit_kind == "real", target


def test_a_path_that_climbs_out_of_the_temp_dir_is_real(tmp_path, salt):
    turn = _turn(tmp_path, [_edit("Edit", "/tmp/../repo/a.py")], [_ok()])
    assert turn.edit_kind == "real"


@pytest.mark.parametrize("order", ["temp first", "temp last"])
def test_a_turn_with_one_temp_and_one_real_edit_is_real(tmp_path, salt, order):
    temp = _edit("Write", TEMP_SLASHED + "/scratch.py", "t1" if order == "temp first" else "t2")
    real = _edit("Edit", "C:/Dev/repo/a.py", "t2" if order == "temp first" else "t1")
    blocks = [temp, real] if order == "temp first" else [real, temp]
    turn = _turn(tmp_path, blocks, [_ok("t1"), _ok("t2")])
    assert turn.edit_kind == "real"


def test_a_turn_whose_edits_are_all_in_the_temp_dir_is_scratch(tmp_path, salt):
    turn = _turn(tmp_path, [
        _edit("Write", TEMP_SLASHED + "/a.py", "t1"),
        _edit("Edit", TEMP + "\\b.py", "t2"),
        _edit("Write", "/tmp/c.py", "t3"),
    ], [_ok("t1"), _ok("t2"), _ok("t3")])
    assert turn.edit_kind == "scratch"


@pytest.mark.parametrize(
    "target, kind",
    [
        (TEMP_SLASHED + "/scratch.ipynb", "scratch"),
        (TEMP + "\\scratch.ipynb", "scratch"),
        ("C:/Dev/repo/analysis.ipynb", "real"),
    ],
    ids=["temp forward slashes", "temp backslashes", "repo"],
)
def test_a_notebook_edit_is_judged_by_its_notebook_path(tmp_path, salt, target, kind):
    turn = _turn(tmp_path, [_edit("NotebookEdit", target)], [_ok()])
    assert turn.edit_kind == kind


def test_a_notebook_edit_with_only_a_file_path_is_not_an_edit_call(tmp_path, salt):
    """NotebookEdit's path key is ``notebook_path``; a ``file_path`` is not read."""
    turn = _turn(tmp_path, [tool_use_block("NotebookEdit", "t1", {"file_path": "C:/Dev/repo/a.ipynb"})], [_ok()])
    assert turn.edit_kind is None


def test_a_turn_with_no_edit_call_has_no_edit_kind(tmp_path, salt):
    turn = _turn(tmp_path, [
        tool_use_block("Read", "t1", {"file_path": TEMP_SLASHED + "/scratch.py"}),
        tool_use_block("Bash", "t2", {"command": "echo x > src/a.py"}),
    ], [_ok("t1"), _ok("t2")])
    assert turn.edit_kind is None


def test_an_edit_call_with_no_path_has_no_edit_kind(tmp_path, salt):
    turn = _turn(tmp_path, [tool_use_block("Edit", "t1", {"old_string": "a", "new_string": "b"})], [_ok()])
    assert turn.edit_kind is None

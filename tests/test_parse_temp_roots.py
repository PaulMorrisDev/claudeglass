"""Which paths are temp (``parse._temp_prefixes``, ``_is_temp_target``,
``_is_code_write_target``): taken from the shape of the path itself, plus
the reading process's own temp folder, so the same transcript counts the
same under any ``TEMP``."""

from __future__ import annotations

import tempfile

import pytest

from claudeglass import parse
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

REPO = "C:/Dev/repo"
#: A temp folder that has nothing to do with the paths below.
ELSEWHERE = "Z:/readers/own-temp"


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _turn(tmp_path, blocks, results):
    path = tmp_path / "t.jsonl"
    reply = turn_line(content=list(blocks), timestamp=_ts(1), cwd=REPO)
    reply["message"]["stop_reason"] = "tool_use"
    final = turn_line(content=[{"type": "text", "text": "done"}], timestamp=_ts(3))
    final["message"]["stop_reason"] = "end_turn"
    write_jsonl(path, [
        user_str_line("go", timestamp=_ts(0), cwd=REPO),
        reply,
        user_block_line(list(results), timestamp=_ts(2)),
        final,
    ])
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    return next(t for t in result.turns if t.tool_use_ids)


def _is_temp(target: str) -> bool:
    return parse._is_temp_target(target, parse._temp_prefixes())


@pytest.fixture(autouse=True)
def own_temp(monkeypatch):
    """The reading process's temp folder is somewhere none of the shapes reach."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: ELSEWHERE)


@pytest.mark.parametrize("target", [
    # Windows, in each spelling a transcript holds
    "C:\\Users\\someone\\AppData\\Local\\Temp\\x.py",
    "C:/Users/someone/AppData/Local/Temp/sub/dir/x.py",
    "c:/users/someone/appdata/local/temp/x.py",
    "/c/Users/someone/AppData/Local/Temp/x.py",
    "D:\\Users\\Ana Maria\\AppData\\Local\\Temp\\x.py",
    "C:\\Windows\\Temp\\x.py",
    "/c/Windows/Temp/x.py",
    # macOS
    "/var/folders/zz/abc123def456/T/x.py",
    "/private/var/folders/zz/abc123def456/T/x.py",
    "/private/tmp/x.py",
    # Linux and Git Bash
    "/tmp/x.py",
    "/tmp/sub/x.py",
    "/var/tmp/x.py",
    # Windows 8.3 short user name
    "C:\\Users\\PAULM~1\\AppData\\Local\\Temp\\x.py",
    "/c/Users/PAULM~1/AppData/Local/Temp/x.py",
    # Windows 11 keeps a service's temp files in SystemTemp
    "C:\\Windows\\SystemTemp\\x.py",
    "/c/Windows/SystemTemp/x.py",
    # The same folders as WSL and Cygwin name them
    "/mnt/c/Users/someone/AppData/Local/Temp/x.py",
    "/mnt/d/Users/PAULM~1/AppData/Local/Temp/sub/x.py",
    "/cygdrive/c/Users/someone/AppData/Local/Temp/x.py",
    "/mnt/c/Windows/Temp/x.py",
    "/cygdrive/c/Windows/Temp/x.py",
    "/mnt/c/Windows/SystemTemp/x.py",
    # macOS /var/tmp as the system really names it
    "/private/var/tmp/x.py",
    # A Windows long-path prefix in front of a Windows shape
    "\\\\?\\C:\\Users\\someone\\AppData\\Local\\Temp\\x.py",
    "\\\\?\\C:\\Windows\\Temp\\x.py",
    "//?/C:/Users/someone/AppData/Local/Temp/x.py",
])
def test_a_path_in_a_known_temp_shape_is_temp_under_any_reader(target):
    assert _is_temp(target)
    assert not parse._is_code_write_target(target, parse._temp_prefixes(), REPO)


@pytest.mark.parametrize("target", [
    "C:/Dev/app/temp/x.py",
    "C:/Dev/app/Temp/x.py",
    "C:/Dev/app/tmp/x.py",
    "/home/ana/app/tmp/x.py",
    "/home/ana/app/temp/x.py",
    "/srv/tmp/x.py",
    "/tmpfiles/x.py",
    "/var/tmpfiles/x.py",
    "C:/Users/someone/AppData/Local/Tempfiles/x.py",
    "C:/Users/someone/AppData/Roaming/Temp/x.py",
    "C:/Users/someone/Documents/AppData/Local/Temp/x.py",
    # A shape counts only from the root of the path: a relative target that resolves inside a project is a
    # project file, whatever folders it is named after
    "C:\\Dev\\proj\\Users\\someone\\AppData\\Local\\Temp\\out.py",
    "/c/Dev/proj/Users/someone/AppData/Local/Temp/out.py",
    "C:/Dev/proj/Windows/Temp/out.py",
    "/home/ana/proj/tmp/out.py",
    "/home/ana/proj/var/tmp/out.py",
    "/home/ana/proj/private/tmp/out.py",
    "/home/ana/proj/var/folders/zz/abc123def456/T/out.py",
    # The same folders named inside a project are not temp, in every spelling
    "C:/Dev/proj/Windows/SystemTemp/out.py",
    "/home/ana/proj/mnt/c/Users/someone/AppData/Local/Temp/out.py",
    "/home/ana/proj/cygdrive/c/Users/someone/AppData/Local/Temp/out.py",
    "/home/ana/proj/mnt/c/Windows/Temp/out.py",
    "/mnt/c/Dev/proj/Users/someone/AppData/Local/Temp/out.py",
    "/cygdrive/c/Dev/proj/Windows/Temp/out.py",
    "/home/ana/proj/private/var/tmp/out.py",
    "C:/Dev/proj/\\\\?\\C:\\Users\\someone\\AppData\\Local\\Temp\\out.py",
    "C:/Dev/\\\\?\\C:\\Windows\\Temp\\out.py",
    # A drive that is not one letter, or a folder at the root of a mounted drive, is not temp
    "/mnt/cc/Users/someone/AppData/Local/Temp/x.py",
    "/mnt/Users/someone/AppData/Local/Temp/x.py",
    "/mnt/c/tmp/x.py",
    "/mnt/c/Temp/x.py",
    "/cygdrive/c/tmp/x.py",
    "C:/Windows/System/Temp/x.py",
    # A folder at the root of a drive is not temp
    "C:\\tmp\\x.py",
    "C:\\Temp\\x.py",
    "C:/tmp/x.py",
    "C:/Windows/Temporary/x.py",
    "C:/Users/someone/Temp/x.py",
    "/var/folders/zz/abc123def456/x.py",
    "/home/ana/tmp/../app/x.py",
    "/tmp/../home/ana/app/x.py",
])
def test_a_folder_that_is_only_named_temp_or_tmp_is_not_temp(target):
    assert not _is_temp(target)
    assert parse._is_code_write_target(target, parse._temp_prefixes(), REPO)


@pytest.mark.parametrize("target", [
    "Z:/readers/own-temp/x.py",
    "Z:\\readers\\own-temp\\sub\\x.py",
    "/z/readers/own-temp/x.py",
])
def test_the_reading_process_own_temp_folder_is_one_more_root(target):
    assert _is_temp(target)


def test_a_directory_that_only_starts_like_the_reading_process_temp_folder_is_not_temp():
    assert not _is_temp("Z:/readers/own-temp2/x.py")


def _counts(turn):
    return (turn.shell_write_count, turn.shell_code_write_count, turn.edit_code_count, turn.edit_kind)


def test_the_same_transcript_counts_the_same_under_two_temp_folders(tmp_path, monkeypatch):
    blocks = [
        tool_use_block("Write", "t1", {"file_path": "C:\\Users\\someone\\AppData\\Local\\Temp\\claude\\a.py"}),
        tool_use_block("Bash", "t2", {"command": "echo x > /c/Users/someone/AppData/Local/Temp/b.py"}),
        tool_use_block("Bash", "t3", {"command": "cat > C:/Users/someone/AppData/Local/Temp/c.py <<'EOF'\nx\nEOF"}),
        tool_use_block("Bash", "t4", {"command": "echo x > src/real.py"}),
    ]
    results = [tool_result_block(f"t{i}", "ok") for i in range(1, 5)]
    counted = []
    for reader_temp in ("D:/jobs/reader-one/tmp", "C:/Users/someone/AppData/Local/Temp"):
        monkeypatch.setattr(tempfile, "gettempdir", lambda reader_temp=reader_temp: reader_temp)
        counted.append(_counts(_turn(tmp_path, blocks, results)))
    assert counted[0] == counted[1]
    # Only the write under src/ is a write to the work.
    assert counted[0][:3] == (1, 1, 0)


def test_the_reading_process_temp_folder_does_not_change_what_a_project_temp_folder_counts(tmp_path, monkeypatch):
    blocks = [tool_use_block("Bash", "t1", {"command": "echo x > C:/Dev/app/temp/x.py"})]
    results = [tool_result_block("t1", "ok")]
    counted = []
    for reader_temp in ("D:/jobs/reader-one/tmp", "C:/Users/someone/AppData/Local/Temp"):
        monkeypatch.setattr(tempfile, "gettempdir", lambda reader_temp=reader_temp: reader_temp)
        counted.append(_counts(_turn(tmp_path, blocks, results)))
    assert counted[0] == counted[1]
    assert counted[0][:2] == (1, 1)


def test_a_relative_target_that_resolves_inside_the_project_is_a_project_file_even_named_like_temp(tmp_path):
    blocks = [
        tool_use_block("Bash", "t1", {"command": "echo x > Users/someone/AppData/Local/Temp/out.py"}),
        tool_use_block("Bash", "t2", {"command": "echo x > /tmp/scratch.py", }),
    ]
    turn = _turn(tmp_path, blocks, [tool_result_block("t1", "ok"), tool_result_block("t2", "ok")])
    assert (turn.shell_write_count, turn.shell_code_write_count) == (1, 1)

"""Reads made through the shell (``shell_reads.read_count``), and what the
parser keeps of them and of Read results: ``Turn.shell_read_count``,
``shell_read_chars`` and ``read_target_chars``.
"""

from __future__ import annotations

import pytest

from claudeglass import parse, shell_reads, shell_writes
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

#: (command, PowerShell, reads)
COMMANDS = [
    ("grep -rn foo src/", False, 1),
    ("cd /c/Dev/x && grep -n foo a.py", False, 1),
    ("rg 'foo bar' src | head -5", False, 1),
    ("sed -n '1,40p' src/a.py", False, 1),
    ("sed --quiet 5p a.py", False, 1),
    ("cat src/a.py", False, 1),
    ("head -20 a.py", False, 1),
    ("tail -n 5 log.txt", False, 1),
    ("find . -name '*.py'", False, 1),
    ("git log --oneline -5", False, 1),
    ("git -C /w --no-pager diff --stat", False, 1),
    ("git show HEAD~1", False, 1),
    ("grep a x; grep b y", False, 2),
    ("for f in a b; do cat $f; done", False, 1),
    ("Get-Content src/a.py -TotalCount 20", True, 1),
    ("Get-Content -Path src/a.py | Select-String foo", True, 1),
    ("Select-String -Path src/*.py -Pattern foo", True, 1),
    ("Set-Location C:/dev; gc a.txt", True, 1),
    ("type a.txt", True, 1),
    # Not reads: it writes, edits in place, deletes, reads standard input or something else
    ("sed -i 's/a/b/' src/a.py", False, 0),
    ("sed -ni '1,2p' a.py", False, 0),
    ("cat > src/a.py <<'EOF'\nhello\nEOF", False, 0),
    ("cat <<'EOF'\nhello\nEOF", False, 0),
    ("cat a.py b.py > c.py", False, 0),
    ("cat | grep x", False, 0),
    ("find . -name '*.pyc' -delete", False, 0),
    ("git diff > x.patch", False, 0),
    ("git status --short", False, 0),
    ("ls -la src", False, 0),
    ("wc -l src/a.py", False, 0),
    ("echo hi | grep h", False, 0),
    ("ls | grep foo", False, 0),
    ("type a.txt", False, 0),
    ("pytest -q", False, 0),
    ("git commit -m 'grep foo'", False, 0),
    ("echo grep foo", False, 0),
    ("", False, 0),
]


@pytest.mark.parametrize("command, powershell, reads", COMMANDS)
def test_which_shell_commands_read_files(command, powershell, reads):
    assert shell_reads.read_count(command, powershell=powershell) == reads


def test_a_pipeline_is_one_read_by_the_command_that_starts_it():
    assert shell_reads.read_count("grep foo a.py | sort | uniq -c | head", powershell=False) == 1
    assert shell_reads.read_count("cat a.py | grep foo | wc -l", powershell=False) == 1


def test_the_tokenizer_reads_pipelines_the_way_the_write_finder_does():
    commands = shell_writes.simple_commands("cd x && cat a.py > b.py | tee c", powershell=False)
    assert [[c.program for c in pipeline] for pipeline in commands] == [["cd"], ["cat", "tee"]]
    assert commands[1][0].redirected and commands[1][0].args == ("a.py",)
    heredoc = shell_writes.simple_commands("cat <<'EOF'\ngrep not a command\nEOF", powershell=False)
    assert [[c.program for c in pipeline] for pipeline in heredoc] == [["cat"]]
    assert heredoc[0][0].fed


# -- what the parser keeps ---------------------------------------------------------


def _at(second: int) -> str:
    return f"2026-09-18T12:00:{second:02d}.000Z"


def _session(tmp_path, calls, results, *, salt=True):
    """One reply making ``calls`` ((tool, input) pairs, ids ``tu_0``...), their ``results`` ((text, is_error, extra) per
    call), and a closing reply. The turns come back."""
    if salt:
        parse.set_salt(b"s" * 32)
    uses = [tool_use_block(tool, f"tu_{i}", given) for i, (tool, given) in enumerate(calls)]
    lines = [
        user_str_line("look around", origin={"kind": "human"}, timestamp=_at(0)),
        turn_line(content=uses, message_id="msg_a", timestamp=_at(1)),
    ]
    for i, (content, is_error, extra) in enumerate(results):
        block = tool_result_block(f"tu_{i}", content, **({"is_error": True} if is_error else {}))
        lines.append(user_block_line([block], timestamp=_at(2 + i), **extra))
    lines.append(turn_line(content=[{"type": "text", "text": "done"}], message_id="msg_b", timestamp=_at(30)))
    path = tmp_path / "s.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path))).turns


def _bash(command: str):
    return ("Bash", {"command": command})


def test_a_turn_counts_its_shell_reads_and_the_size_of_what_came_back(tmp_path):
    out_a, out_b = "a.py:1:foo\n" * 3, "b" * 500
    turns = _session(
        tmp_path,
        [_bash("grep -rn foo src/"), _bash("ls"), _bash("cd app && cat src/b.py"), _bash("pytest -q")],
        [(out_a, False, {}), ("x" * 99, False, {}), (out_b, False, {}), ("1 passed", False, {})],
    )
    assert turns[0].shell_read_count == 2
    # The listing and the test run are not reads, so their output is not counted.
    assert turns[0].shell_read_chars == len(out_a) + len(out_b)
    assert (turns[1].shell_read_count, turns[1].shell_read_chars) == (0, 0)


def test_a_read_through_powershell_counts_like_one_through_bash(tmp_path):
    turns = _session(
        tmp_path, [("PowerShell", {"command": "Get-Content src/a.py -TotalCount 5"})], [("line\n" * 5, False, {})]
    )
    assert (turns[0].shell_read_count, turns[0].shell_read_chars) == (1, 25)


def test_a_search_that_found_nothing_still_counts_and_its_output_is_in_context(tmp_path):
    turns = _session(tmp_path, [_bash("grep -rn nope src/")], [("Exit code 1", True, {})])
    assert (turns[0].shell_read_count, turns[0].shell_read_chars) == (1, len("Exit code 1"))


def test_a_read_a_hook_blocked_or_you_denied_never_happened(tmp_path):
    blocked = "PreToolUse:Bash hook error: [guard]: use the Grep tool"
    denied = "The user doesn't want to proceed with this tool use. The tool use was rejected."
    for text in (blocked, denied):
        turns = _session(
            tmp_path, [_bash("grep -rn foo src/"), _bash("cat src/b.py")], [(text, True, {}), ("body", False, {})]
        )
        assert (turns[0].shell_read_count, turns[0].shell_read_chars) == (1, 4), text


def test_a_read_target_keeps_the_size_of_its_result_beside_its_hash(tmp_path):
    turns = _session(
        tmp_path,
        [("Read", {"file_path": "/w/a.py"}), ("Read", {"file_path": "/w/missing.py"}), ("Read", {"file_path": "/w/c.py"})],
        [("x" * 120, False, {}), ("File does not exist.", True, {}), ([{"type": "text", "text": "y" * 40}], False, {})],
    )
    turn = turns[0]
    assert len(turn.read_target_hashes) == 3
    # A failed Read read nothing.
    assert turn.read_target_chars == (120, 0, 40)


def test_a_read_target_needs_the_salt_like_its_hash_does(tmp_path):
    turns = _session(tmp_path, [("Read", {"file_path": "/w/a.py"})], [("x" * 10, False, {})], salt=False)
    assert turns[0].read_target_hashes == () and turns[0].read_target_chars == ()


def test_a_result_claude_code_saved_to_a_file_counts_as_the_preview_left_in_context(tmp_path):
    persisted = (
        "<persisted-output>\nOutput too large (214.3KB). Full output saved to: /tmp/tool-results/abc.txt\n\n"
        "Preview (first 2KB):\n" + "line of the file\n" * 100 + "...\n</persisted-output>"
    )
    turns = _session(
        tmp_path,
        [("Read", {"file_path": "/w/big.py"}), _bash("cat /w/big.log")],
        [(persisted, False, {}), (persisted, False, {})],
    )
    assert turns[0].read_target_chars == (len(persisted),)
    assert turns[0].shell_read_chars == len(persisted)
    assert len(persisted) < 214_000

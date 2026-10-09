"""Which shell commands run the tests, and how much of them
(``testrun.run_scope``): the one matcher the parser (``Turn.tests_run``),
the purpose rules (``classify._matches_test_tool``) and the capture hook
share, and the hook's own copy of it, held to the package's.
"""

from __future__ import annotations

import importlib.util
import json
from importlib import resources
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat, classify, testrun
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

SCRIPT = Path(str(resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT))
MODULE = SCRIPT.with_name(cat.HOOK_MODULE)


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("_testrun_hook_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook_module()

#: Commands as Claude Code transcripts hold them, and what each runs.
COMMANDS = [
    # Plain
    ("pytest", "full"),
    ("python -m pytest -q", "full"),
    ("python -m pytest -q tests/test_a.py", "targeted"),
    ("pytest tests/test_store.py::test_remove -x", "targeted"),
    ("pytest -k remove", "targeted"),
    # A whole folder is still the whole suite, and so are options that take a number or a file.
    ("pytest tests/", "full"),
    ("pytest tests", "full"),
    ("python -m pytest -n 4 -q", "full"),
    ("python -m pytest --cov src -q", "full"),
    ("pytest --junitxml=out.xml", "full"),
    ("pytest -q 2>&1 | tail -20", "full"),
    # Asking what would run, or for help, runs nothing.
    ("pytest --collect-only -q", ""),
    ("pytest --help", ""),
    # Interpreter paths, env and timeout prefixes, quoted paths, PowerShell
    ("C:/Python311/python.exe -m pytest -q tests/test_a.py tests/test_b.py", "targeted"),
    ("C:\\Python311\\python.exe -m pytest -q", "full"),
    ("& \"C:\\Python311\\python.exe\" -m pytest tests\\test_a.py", "targeted"),
    ("& .venv\\Scripts\\pytest.exe -q", "full"),
    ("\"/c/Program Files/Python311/python.exe\" -m pytest -q", "full"),
    (".venv/Scripts/python -m pytest tests/test_a.py", "targeted"),
    ("PYTHONPATH=src python -m pytest -q", "full"),
    ("FOO=1 BAR=2 timeout 120 uv run pytest -x", "full"),
    ("timeout 300 python3 -m pytest tests/test_a.py", "targeted"),
    ("uv run --with pytest pytest", "full"),
    ("poetry run pytest -k parse", "targeted"),
    ("$env:PYTHONPATH='src'; python -m pytest -q", "full"),
    ("Set-Location C:\\Dev\\app; python -m pytest tests\\test_a.py", "targeted"),
    # Compound lines: a whole run anywhere makes it a whole run
    ("cd /c/Dev/app && python -m pytest -q", "full"),
    ("(cd app && npm test)", "full"),
    ("cd app && pytest tests/test_a.py && pytest", "full"),
    ("pytest tests/test_a.py; pytest tests/test_b.py", "targeted"),
    ("for f in a b; do pytest tests/test_$f.py; done", "targeted"),
    ("git status && pytest -q | tee out.txt", "full"),
    # Other runners
    ("npm test", "full"),
    ("npm run test -- --testPathPattern=cart", "targeted"),
    ("npx jest src/cart.spec.js", "targeted"),
    ("npx vitest run", "full"),
    ("yarn test", "full"),
    ("go test ./...", "full"),
    ("go test ./pkg/cart", "targeted"),
    ("go test -run TestCart ./...", "targeted"),
    ("cargo test", "full"),
    ("cargo test parse_", "targeted"),
    ("cargo test --release", "full"),
    ("dotnet test", "full"),
    ("dotnet test tests/App.Tests/App.Tests.csproj", "targeted"),
    ("dotnet test --filter Name~Cart", "targeted"),
    ("dotnet test --list-tests", ""),
    ("mvn test", "full"),
    ("./gradlew test", "full"),
    ("make test", "full"),
    ("tox -e py311", "full"),
    # Not a run: the runner's name elsewhere, or a script or message that mentions it
    ("grep -r pytest .", ""),
    ("echo pytest", ""),
    ("git commit -m 'run pytest'", ""),
    # A quoted ; or && is no end of a command, however the string reads
    ("git commit -m \"Fix parser; pytest passes now\"", ""),
    ("echo 'done; pytest -q'", ""),
    ("echo \"ran it && pytest -q\"", ""),
    ("git commit -m \"it's fixed; pytest -q\"", ""),
    ("dotnet test --filter \"Name~A|Name~B\" --list-tests", ""),
    # A string handed to a shell's -c is commands, not a message
    ("docker run --rm img sh -c \"cd /app && pytest -q\"", "full"),
    ("bash -lc 'cd sub; npm test'", "full"),
    ("pwsh -NoProfile -Command \"cd x; python -m pytest tests/test_a.py\"", "targeted"),
    ("cmd /c \"cd x && dotnet test\"", "full"),
    ("./run.sh -c \"a; pytest\"", ""),
    ("ssh host -c aes128-ctr \"echo; pytest\"", ""),
    # ... but a real run after the quoted string is one, and a quoted path may hold a bracket
    ("git commit -m \"Fix parser; ok\" && pytest -q", "full"),
    ("& \"C:\\Program Files (x86)\\Python311\\python.exe\" -m pytest -q", "full"),
    ("dotnet test --filter \"Name~A|Name~B\"", "targeted"),
    ("pip install pytest", ""),
    ("python -m inventory.cli list stock.csv", ""),
    ("cat tests/test_a.py", ""),
    ("ls tests", ""),
    ("cat > run.sh <<'EOF'\npytest tests/test_a.py\nEOF", ""),
    ("git commit -m \"$(cat <<'EOF'\nAdd tests; pytest passes\nEOF\n)\"", ""),
    ("", ""),
]


@pytest.mark.parametrize("command, scope", COMMANDS)
def test_which_commands_run_tests_and_how_much(command, scope):
    assert testrun.run_scope(command) == scope


@pytest.mark.parametrize("command, scope", COMMANDS)
def test_the_hooks_copy_reads_every_command_as_the_package_does(command, scope):
    assert HOOK._test_scope(command) == testrun.run_scope(command)


def test_the_hook_reads_its_patterns_from_the_catalogue():
    coaching = HOOK.load_catalogue()["coaching"]
    for name in ("command_split", "heredoc", "quoted", "prefix", "program", "runner", "no_run", "target",
                 "bare_target", "bare_target_runner", "no_target", "whole_suite"):
        assert coaching[f"test_{name}_pattern"] == getattr(cat, f"TEST_{name.upper()}_PATTERN")
    # The packaged JSON is the one the hook reads, and it is up to date.
    packaged = json.loads(Path(SCRIPT).with_name(cat.CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert packaged["coaching"]["test_runner_pattern"] == cat.TEST_RUNNER_PATTERN


def test_a_session_ran_the_widest_of_its_runs():
    assert testrun.widest([]) == ""
    assert testrun.widest(["targeted"]) == "targeted"
    assert testrun.widest(["targeted", "full"]) == "full"
    assert testrun.widest(iter(["", "targeted", ""])) == "targeted"


def test_the_matcher_is_not_named_so_pytest_collects_it():
    assert not testrun.run_scope.__name__.startswith("test")
    assert not testrun.__name__.rsplit(".", 1)[-1].startswith("test_")


@pytest.mark.parametrize("prefix, runs", [
    ("pytest -q", True),
    ("dotnet test", True),
    ("C:/Python311/python.exe -m pytest -q", True),
    ("cd /c/Dev/app && python -m pytest tests/test_a.py", True),
    ("FOO=1 timeout 60 uv run pytest", True),
    ("grep -r pytest .", False),
    ("git status", False),
    ("", False),
])
def test_the_purpose_rules_ask_the_same_matcher(prefix, runs):
    assert classify._matches_test_tool(prefix) is runs


# -- what the parser keeps ---------------------------------------------------------


def _at(second: int) -> str:
    return f"2026-09-18T12:00:{second:02d}.000Z"


def _run(tmp_path, *calls, results=(), more=()):
    """One reply that makes ``calls`` (tool, command) and the results that answer them, then a closing reply."""
    uses = [tool_use_block(tool, f"tu_{i}", {"command": command}) for i, (tool, command) in enumerate(calls)]
    lines = [
        user_str_line("run the tests", origin={"kind": "human"}, timestamp=_at(0)),
        turn_line(content=uses, message_id="msg_a", timestamp=_at(1)),
        *more,
    ]
    for i, (text, error) in enumerate(results):
        lines.append(user_block_line(
            [tool_result_block(f"tu_{i}", text, **({"is_error": True} if error else {}))], timestamp=_at(2 + i)
        ))
    lines.append(turn_line(content=[{"type": "text", "text": "done"}], message_id="msg_b", timestamp=_at(9)))
    path = tmp_path / "s.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path))).turns


def test_a_turn_records_how_much_of_the_tests_it_ran(tmp_path):
    turns = _run(tmp_path, ("Bash", "python -m pytest -q tests/test_a.py"), results=[("1 passed", False)])
    assert [t.tests_run for t in turns] == ["targeted", ""]


def test_the_widest_run_of_a_turn_is_kept(tmp_path):
    turns = _run(
        tmp_path, ("Bash", "pytest tests/test_a.py"), ("Bash", "cd app && python -m pytest -q"), ("Bash", "ls"),
        results=[("1 passed", False), ("9 passed", False), ("a", False)],
    )
    assert turns[0].tests_run == "full"


def test_a_powershell_run_counts_too(tmp_path):
    turns = _run(tmp_path, ("PowerShell", "& \"C:\\Python311\\python.exe\" -m pytest tests\\test_a.py"))
    assert turns[0].tests_run == "targeted"


def test_a_failing_test_run_is_still_a_run(tmp_path):
    turns = _run(tmp_path, ("Bash", "pytest -q"), results=[("Exit code 1\n1 failed", True)])
    assert turns[0].tests_run == "full"


def test_a_run_a_hook_blocked_or_you_denied_never_happened(tmp_path):
    blocked = "PreToolUse:Bash hook error: [guard]: no tests here"
    denied = "The user doesn't want to proceed with this tool use. The tool use was rejected."
    for text in (blocked, denied):
        turns = _run(tmp_path, ("Bash", "pytest -q"), results=[(text, True)])
        assert turns[0].tests_run == "", text


def test_a_command_that_only_mentions_the_tests_runs_none(tmp_path):
    turns = _run(tmp_path, ("Bash", "git commit -m 'run pytest tests/test_a.py'"))
    assert turns[0].tests_run == ""


def test_a_quoted_message_that_names_a_runner_after_a_semicolon_runs_none_for_the_parser_and_the_rules(tmp_path):
    message = "git commit -m \"Refactor parser; pytest green\""
    assert _run(tmp_path, ("Bash", message))[0].tests_run == ""
    assert classify._matches_test_tool(message) is False


@pytest.mark.parametrize("command, scope", COMMANDS)
def test_the_hooks_copy_cuts_every_command_apart_as_the_package_does(command, scope):
    assert HOOK._command_parts(command) == testrun.command_parts(command)


@pytest.mark.parametrize("command, parts", [
    ("git commit -m 'a' && pytest -q tests/a.py", ["git commit -m 'a'", "pytest -q tests/a.py"]),
    ("FOO=1 timeout 60 uv run pytest", ["pytest"]),
    ("C:\\Python311\\python.exe -m pytest; npm test", ["python -m pytest", "npm test"]),
    ("ls | grep a", ["ls", "grep a"]),
    # The operators inside a quoted string are blanked; those outside it still cut.
    ("git commit -m \"a; b\" && pytest", ["git commit -m \"a  b\"", "pytest"]),
    ("echo 'x | y' | tee out.txt", ["echo 'x   y'", "tee out.txt"]),
    # ... but a shell's -c script loses only its quotes, so its commands are cut apart.
    ("docker exec c sh -c \"cd /app && pytest\"", ["docker exec c sh -c cd /app", "pytest"]),
    # A heredoc's body is not a command.
    ("cat > a.txt <<'EOF'\nrm -rf x\nEOF\nnpm test", ["cat > a.txt", "npm test"]),
    ("", [""]),
])
def test_a_line_is_cut_into_its_commands_with_what_comes_before_each_program_removed(command, parts):
    assert [part.strip() for part in testrun.command_parts(command)] == parts


@pytest.mark.parametrize("part, program", [
    ("  git merge main", "git merge main"),
    ("(cd sub", "cd sub"),
    ("PYTHONPATH=src BAR=2 python -m pytest", "python -m pytest"),
    ("timeout 120 uv run pytest -x", "pytest -x"),
    ("sudo rm -rf build", "rm -rf build"),
    ("/usr/bin/git pull", "git pull"),
    ("C:/Python311/python.exe -m pytest -q", "python -m pytest -q"),
])
def test_one_command_loses_what_comes_before_its_program_and_the_folder_and_exe_of_the_program(part, program):
    assert testrun.normalize(part) == program

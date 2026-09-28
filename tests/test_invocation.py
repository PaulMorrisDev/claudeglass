"""The command the dashboard prints for this install (invocation.py).

The short ``claudeglass`` only runs when pip's Scripts folder is
on ``PATH``; a default Windows Python install leaves it off, so the
service names the form that runs here and swaps it into every command
it serves. conftest.py pins ``CLAUDEGLASS_COMMAND`` to the short
form for every other test; these tests clear or set it themselves.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from claudeglass import installer, invocation

from test_service_api import _start_server


@pytest.fixture
def detect(monkeypatch):
    """Run the detection fresh, with no override and a controlled PATH
    lookup: ``which`` maps a name to a path (missing names are None)."""
    monkeypatch.delenv(invocation.ENV_VAR, raising=False)
    invocation._detected_prefix.cache_clear()
    found: dict[str, str] = {}
    monkeypatch.setattr(invocation.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(installer, "detect_pyz_path", lambda: None)

    def run(*, executable: str, which: dict[str, str] | None = None, pyz: Path | None = None) -> str:
        found.clear()
        found.update(which or {})
        monkeypatch.setattr(invocation.sys, "executable", executable)
        monkeypatch.setattr(installer, "detect_pyz_path", lambda: pyz)
        invocation._detected_prefix.cache_clear()
        return invocation.command_prefix()

    yield run
    invocation._detected_prefix.cache_clear()


def _scripts_dir(monkeypatch, folder: Path) -> None:
    monkeypatch.setattr(invocation.sysconfig, "get_path", lambda name, scheme=None: str(folder) if scheme is None else "")


# -- detection -----------------------------------------------------------


def test_the_env_var_wins_word_for_word(monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, "  tl  ")
    assert invocation.command_prefix() == "tl"


def test_an_empty_env_var_falls_back_to_the_detection(detect, monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, "   ")
    python = str(Path("/opt/py/bin/python3"))
    assert detect(executable=python) == f"{python} -m claudeglass"


def test_the_launcher_in_this_pythons_scripts_folder_keeps_the_short_form(detect, monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    _scripts_dir(monkeypatch, scripts)
    launcher = str(scripts / "claudeglass.exe")
    assert detect(executable=str(tmp_path / "python.exe"), which={"claudeglass": launcher}) == "claudeglass"


def test_a_launcher_from_another_python_is_not_trusted(detect, monkeypatch, tmp_path):
    # An older install elsewhere on PATH would run different code.
    _scripts_dir(monkeypatch, tmp_path / "mine" / "Scripts")
    python = str(tmp_path / "mine" / "python.exe")
    other = str(tmp_path / "other" / "Scripts" / "claudeglass.exe")
    prefix = detect(executable=python, which={"claudeglass": other})
    assert prefix.endswith(" -m claudeglass")
    assert other not in prefix and python in prefix


def test_no_launcher_and_python_on_path_is_this_one(detect, tmp_path):
    python = str(tmp_path / "python.exe")
    assert detect(executable=python, which={"python": python}) == "python -m claudeglass"


def test_python3_on_path_counts_when_python_is_missing(detect, tmp_path):
    python = str(tmp_path / "bin" / "python3")
    assert detect(executable=python, which={"python3": python}) == "python3 -m claudeglass"


def test_a_different_python_on_path_means_the_full_path(detect, tmp_path):
    # The user's case: the service runs one Python, PATH finds another
    # (or none), so "python -m" would miss the package.
    python = str(tmp_path / "Python314" / "python.exe")
    other = str(tmp_path / "WindowsApps" / "python.exe")
    assert detect(executable=python, which={"python": other}) == f"{python} -m claudeglass"


def test_the_pyz_runs_through_this_python(detect, tmp_path):
    python = str(tmp_path / "python.exe")
    pyz = tmp_path / "claudeglass.pyz"
    assert detect(executable=python, which={"python": python}, pyz=pyz) == f"python {pyz}"


def test_the_pyz_wins_over_a_launcher_on_path(detect, monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    _scripts_dir(monkeypatch, scripts)
    python = str(tmp_path / "python.exe")
    pyz = tmp_path / "claudeglass.pyz"
    which = {"python": python, "claudeglass": str(scripts / "claudeglass.exe")}
    assert detect(executable=python, which=which, pyz=pyz) == f"python {pyz}"


def test_pythonw_becomes_the_console_python_beside_it(detect, tmp_path):
    # The logon service runs windowless; pythonw prints nothing to a terminal.
    (tmp_path / "python.exe").write_bytes(b"")
    pythonw = str(tmp_path / "pythonw.exe")
    assert detect(executable=pythonw) == f"{tmp_path / 'python.exe'} -m claudeglass"


def test_pythonw_stays_when_no_console_python_sits_beside_it(detect, tmp_path):
    pythonw = str(tmp_path / "pythonw.exe")
    assert detect(executable=pythonw) == f"{pythonw} -m claudeglass"


def test_the_detection_is_worked_out_once(detect, monkeypatch, tmp_path):
    python = str(tmp_path / "python.exe")
    assert detect(executable=python, which={"python": python}) == "python -m claudeglass"
    monkeypatch.setattr(invocation.sys, "executable", str(tmp_path / "moved.exe"))
    assert invocation.command_prefix() == "python -m claudeglass"


@pytest.mark.parametrize(
    ("platform", "path", "expected"),
    [
        ("win32", r"C:\Python314\python.exe", r"C:\Python314\python.exe"),
        ("win32", r"C:\Program Files\Python314\python.exe", r'& "C:\Program Files\Python314\python.exe"'),
        ("linux", "/opt/py/bin/python3", "/opt/py/bin/python3"),
        ("linux", "/home/me/my py/bin/python3", "'/home/me/my py/bin/python3'"),
    ],
)
def test_a_path_is_quoted_only_when_it_needs_it(monkeypatch, platform, path, expected):
    monkeypatch.setattr(invocation.sys, "platform", platform)
    assert invocation._quote(path) == expected


def test_the_real_detection_names_a_form_that_runs_here(monkeypatch):
    monkeypatch.delenv(invocation.ENV_VAR, raising=False)
    invocation._detected_prefix.cache_clear()
    try:
        prefix = invocation.command_prefix()
    finally:
        invocation._detected_prefix.cache_clear()
    assert prefix == "claudeglass" or prefix.endswith((" -m claudeglass", ".pyz", '.pyz"'))
    assert "\n" not in prefix


# -- rewrite -------------------------------------------------------------

PREFIX = r'& "C:\Program Files\Python314\python.exe" -m claudeglass'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("claudeglass capture connect", f"{PREFIX} capture connect"),
        ("Run 'claudeglass capture connect' to add them.", f"Run '{PREFIX} capture connect' to add them."),
        ("claudeglass apply --dry-run", f"{PREFIX} apply --dry-run"),
        ("claudeglass --version", f"{PREFIX} --version"),
        ("First:\nclaudeglass report", f"First:\n{PREFIX} report"),
        ("(claudeglass report)", f"({PREFIX} report)"),
        # Prose, file names and made-up words stay as they are.
        ("claudeglass ships with no dependencies.", "claudeglass ships with no dependencies."),
        ("Download claudeglass.pyz first.", "Download claudeglass.pyz first."),
        ("claudeglass capture-foo", "claudeglass capture-foo"),
        ("claudeglass reports", "claudeglass reports"),
        ("~/.claudeglass capture", "~/.claudeglass capture"),
        ("my-claudeglass capture", "my-claudeglass capture"),
        # A message's label names the command that is talking.
        ("claudeglass update: installed.", "claudeglass update: installed."),
        ("claudeglass serve --purge: will delete", "claudeglass serve --purge: will delete"),
        ("claudeglass capture:\n", "claudeglass capture:\n"),
        # A colon further on is part of the command.
        ("claudeglass apply --revert 2026-09-25T10:00:00", f"{PREFIX} apply --revert 2026-09-25T10:00:00"),
    ],
)
def test_rewrite_swaps_only_commands(text, expected):
    assert invocation.rewrite(text, PREFIX) == expected


def test_rewrite_leaves_every_subcommand_reachable():
    from claudeglass.cli import SUBCOMMANDS

    for word in SUBCOMMANDS:
        assert invocation.rewrite(f"claudeglass {word}", "tl") == f"tl {word}", word


def test_the_short_form_changes_nothing():
    value = {"a": ["claudeglass report"]}
    assert invocation.rewrite_payload(value, "claudeglass") is value


def test_rewrite_payload_reaches_nested_strings_and_leaves_keys():
    value = {
        "claudeglass report": "claudeglass report",
        "rows": [["claudeglass baseline", 3, None, True], ("claudeglass sessions",)],
        "n": 1.5,
    }
    assert invocation.rewrite_payload(value, "tl") == {
        "claudeglass report": "tl report",
        "rows": [["tl baseline", 3, None, True], ["tl sessions"]],
        "n": 1.5,
    }


def test_rewrite_rendered_json_keeps_the_rendering():
    text = json.dumps({"b": "claudeglass report\nclaudeglass baseline", "a": 1}, sort_keys=True, indent=2)
    out = invocation.rewrite_rendered(text, "json", PREFIX)
    assert json.loads(out) == {"a": 1, "b": f"{PREFIX} report\n{PREFIX} baseline"}
    assert out == json.dumps(json.loads(out), sort_keys=True, indent=2)


def test_rewrite_rendered_html_escapes_the_prefix():
    out = invocation.rewrite_rendered("<code>claudeglass report</code>", "html", PREFIX)
    assert out == f"<code>{html.escape(PREFIX, quote=True)} report</code>"


def test_rewrite_rendered_markdown_and_the_short_form():
    assert invocation.rewrite_rendered("Run `claudeglass report`.", "markdown", "tl") == "Run `tl report`."
    text = "<p>claudeglass report</p>"
    assert invocation.rewrite_rendered(text, "html", invocation.SHORT) is text


def test_rewriting_stream_rewrites_what_it_writes_and_passes_the_rest_on():
    import io

    target = io.StringIO()
    stream = invocation.RewritingStream(target, "tl")
    stream.write("Run claudeglass report.\n")
    stream.writelines(["claudeglass update: done.\n", "claudeglass baseline\n"])
    stream.flush()
    assert stream.getvalue() == "Run tl report.\nclaudeglass update: done.\ntl baseline\n"


@pytest.mark.parametrize(
    ("platform", "argv", "expected"),
    [
        (
            "win32",
            [r"C:\Program Files\Py\python.exe", "-m", "pip", "install", "git+https://example.test/x"],
            r'& "C:\Program Files\Py\python.exe" -m pip install git+https://example.test/x',
        ),
        ("win32", [r"C:\Py\python.exe", "--projects-root", r"C:\my work"], r'C:\Py\python.exe --projects-root "C:\my work"'),
        ("linux", ["/usr/bin/python3", "--config-dir", "/home/me/my dir"], "/usr/bin/python3 --config-dir '/home/me/my dir'"),
        ("linux", [], ""),
    ],
)
def test_shell_line_quotes_what_needs_it(monkeypatch, platform, argv, expected):
    monkeypatch.setattr(invocation.sys, "platform", platform)
    assert invocation.shell_line(argv) == expected


# -- the CLI prints it ----------------------------------------------------


@pytest.fixture
def cli_form(monkeypatch):
    from claudeglass import cli

    monkeypatch.setenv(invocation.ENV_VAR, "tl")
    return cli


def test_the_cli_prints_commands_in_this_installs_form(cli_form, monkeypatch, capsys):
    import sys

    def fake(args):
        print("Next: claudeglass capture status")
        print("claudeglass pricing-check: done", file=sys.stderr)
        return 0

    monkeypatch.setattr(cli_form, "_cmd_pricing_check", fake)
    before = sys.stdout, sys.stderr
    assert cli_form.main(["pricing-check"]) == 0
    assert (sys.stdout, sys.stderr) == before
    out, err = capsys.readouterr()
    assert out == "Next: tl capture status\n"
    assert err == "claudeglass pricing-check: done\n"


def test_the_cli_runs_with_no_console_streams(cli_form, monkeypatch):
    # pythonw, which the logon service runs under, has no stdout or
    # stderr: print() writes nothing there, and the dashboard must start.
    import sys

    printed = []

    def fake(args):
        print("Serving on http://127.0.0.1:8765 - claudeglass report")
        print("claudeglass serve: started", file=sys.stderr)
        printed.append(True)
        return 0

    monkeypatch.setattr(cli_form, "_cmd_pricing_check", fake)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert cli_form.main(["pricing-check"]) == 0
    assert printed == [True]
    assert sys.stdout is None and sys.stderr is None


def test_cli_help_comes_in_this_installs_form(cli_form, capsys):
    import sys

    before = sys.stdout, sys.stderr
    with pytest.raises(SystemExit):
        cli_form.main(["capture", "--help"])
    assert (sys.stdout, sys.stderr) == before
    out = capsys.readouterr().out
    assert out.startswith("usage: tl capture ")
    assert "'tl capture status'" in out
    assert not invocation._command_pattern().search(out)


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (["report", "--json"], "_cmd_report_like"),
        (["statusline"], "_cmd_statusline"),
        (["export"], "_cmd_export"),
        (["snapshot-config"], "_cmd_snapshot_config"),
        (["scrub-fixture"], "_cmd_scrub_fixture"),
    ],
)
def test_data_output_is_printed_as_written(cli_form, monkeypatch, capsys, argv, handler):
    def fake(args, **_kwargs):
        print('{"command": "claudeglass report"}')
        return 0

    monkeypatch.setattr(cli_form, handler, fake)
    cli_form.main(argv)
    assert capsys.readouterr().out == '{"command": "claudeglass report"}\n'


# -- the service serves it ------------------------------------------------

# A path with a space and a quote: it must survive JSON and HTML intact.
SERVED = '& "C:\\Program Files\\Py\'s\\python.exe" -m claudeglass'


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv(invocation.ENV_VAR, SERVED)
    handle = _start_server(tmp_path, monkeypatch)
    try:
        yield handle
    finally:
        handle.close()
        handle.store.close()


def test_api_commands_come_in_this_installs_form(served):
    resp, payload = served.post_json("/api/capture", {"level": "essentials"})
    assert resp.status == 200
    assert payload["data"]["hooks"]["connect_command"] == f"{SERVED} capture connect"


def test_the_page_carries_the_form_for_the_dashboards_own_commands(served):
    resp, raw = served.request("GET", "/")
    assert resp.status == 200
    page = raw.decode("utf-8")
    assert f'<meta name="cg-command" content="{html.escape(SERVED, quote=True)}">' in page
    assert 'content="claudeglass"' not in page


def test_report_json_stays_valid_json_with_the_form_swapped_in(served):
    from claudeglass.service import api as service_api

    rendered = json.dumps({"report": {"notes": ["Run claudeglass baseline.\nclaudeglass report"]}})
    text = service_api._report_json_commands(rendered)
    assert json.loads(text) == {"report": {"notes": [f"Run {SERVED} baseline.\n{SERVED} report"]}}
    # Rendered the way render_json renders it.
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=2)

    resp, raw = served.request("GET", "/api/report.json")
    assert resp.status == 200
    leftover = [s for s in _strings(json.loads(raw)) if invocation._command_pattern().search(s)]
    assert leftover == []


@pytest.mark.parametrize(("path", "form"), [("/api/report.md", SERVED), ("/api/report.html", html.escape(SERVED, quote=True))])
def test_the_markdown_and_html_reports_come_in_this_installs_form(served, monkeypatch, path, form):
    from claudeglass.service import api as service_api

    rendered = service_api.invocation.rewrite_rendered
    seen = []

    def spy(text, kind, prefix=None):
        seen.append(kind)
        return rendered(text + "\nclaudeglass baseline", kind, prefix)

    monkeypatch.setattr(service_api.invocation, "rewrite_rendered", spy)
    resp, raw = served.request("GET", path)
    assert resp.status == 200
    text = raw.decode("utf-8")
    assert seen == ["markdown" if path.endswith(".md") else "html"]
    assert text.endswith(f"\n{form} baseline")
    assert not invocation._command_pattern().search(text)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_index_placeholder_matches_the_one_the_service_replaces():
    from claudeglass.service import api as service_api

    index = Path(invocation.__file__).parent / "service" / "static" / "index.html"
    assert index.read_bytes().count(service_api._COMMAND_META) == 1

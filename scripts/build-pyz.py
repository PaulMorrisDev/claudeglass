#!/usr/bin/env python3
"""Build ``dist/claudeglass.pyz`` -- a single-file, dependency-free
distribution of claudeglass (``docs/deploy.md``'s ".pyz build"
path), for a machine where ``pip install`` is unavailable or unwanted.

Usage::

    python scripts/build-pyz.py [--output PATH]
    python scripts/build-pyz.py --smoke PATH

``--smoke`` builds nothing: it runs the built archive the way a user
would (see :func:`smoke`) and exits 1, naming each failure, when it
cannot serve the dashboard, read the shipped profiles or print a
subcommand's help. The release workflow runs it before the archive is
attached to a release.

Uses the stdlib :mod:`zipapp` module directly (rather than shelling out
to ``python -m zipapp``) so this script has a normal argparse CLI and a
testable ``build()`` function, but the archive it produces is bit-for-bit
what ``python -m zipapp src -m "claudeglass.__main__:main" -o
dist/claudeglass.pyz -p "/usr/bin/env python3" -c`` would produce
from this repository's own ``src/`` layout. The archive is compressed
(``-c``), which is a size saving only: ``zipimport`` and
``importlib.resources`` read a compressed member the same way.

Why ``claudeglass.__main__:main``, not ``claudeglass.cli:main``:
``zipapp``'s generated archive-root ``__main__.py`` (built from the
``main=`` argument) is just::

    import claudeglass.__main__
    claudeglass.__main__.main()

``claudeglass/__main__.py`` itself is a *module*, not a function --
its own top-level statement is ``sys.exit(main())`` (see that file),
which runs the instant ``import claudeglass.__main__`` executes,
propagating ``cli.main()``'s real exit code via the ``SystemExit`` that
raises. Pointing zipapp at ``claudeglass.cli:main`` instead would
produce a wrapper that calls ``cli.main()`` and discards its returned
int, silently exiting 0 regardless of what the CLI actually returned --
exactly the bug ``__main__.py``'s own docstring warns about for `python
-m claudeglass`` itself. Targeting ``__main__:main`` sidesteps
that: the module import raises ``SystemExit`` with the real code before
the generated wrapper's own ``.main()`` call is ever reached.

Only ``src/claudeglass/`` is archived -- no ``tests/``, no build
tooling, no repository metadata -- so the archive is exactly what a
``pip install .`` of this package would have put on `sys.path`, nothing
more. ``service/static/*`` (the web UI) is an ordinary package-data
tree already living under ``src/claudeglass/service/static/``, so
it is included automatically; no separate step is needed.
"""

from __future__ import annotations

import argparse
import compileall
import json
import py_compile
import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "claudeglass"
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "claudeglass.pyz"

#: Never let a stray build artefact from the developer's own environment
#: end up inside the shipped archive: byte-code caches, and any dot-file
#: or dot-folder (an editor's or a tool's own cache, which may hold local
#: paths).
_EXCLUDED_DIR_NAMES = {"__pycache__"}


def _copy_source_tree(dest: Path) -> None:
    """Copy ``src/claudeglass`` into ``dest`` (a fresh temp
    directory), skipping ``__pycache__`` and dot-files -- ``zipapp.create_archive``
    has no include/exclude filter of its own, so this is done with a
    plain filtered copy first rather than archiving ``src/`` in place.
    """

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _EXCLUDED_DIR_NAMES or name.startswith(".")}

    shutil.copytree(PACKAGE_DIR, dest / "claudeglass", ignore=_ignore)


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    """Build the ``.pyz`` at ``output`` (creating parent directories as
    needed) and return its path.
    """
    if not PACKAGE_DIR.is_dir():
        raise FileNotFoundError(f"expected package source at {PACKAGE_DIR}, not found")

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="claudeglass-pyz-") as tmp:
        tmp_path = Path(tmp)
        _copy_source_tree(tmp_path)
        # ROB-P10: byte-compile before archiving, so the shipped .pyz
        # carries .pyc files and zipimport never has to parse and
        # compile every .py from inside the zip on each run it starts.
        # legacy=True writes "module.pyc" next to "module.py" (no
        # __pycache__ directory -- test_built_pyz_excludes_pycache_and_tests
        # forbids one, and zipimport only ever reads that legacy layout
        # from inside a zip, never PEP 3147's __pycache__ one).
        # UNCHECKED_HASH: source and bytecode are built together right
        # here and shipped as one immutable archive, so there is nothing
        # to invalidate against at import time -- a timestamp-based pyc
        # would depend on mtimes surviving the copy and the zip write
        # unchanged, which zipapp makes no promise about.
        compiled_ok = compileall.compile_dir(
            str(tmp_path),
            quiet=1,
            legacy=True,
            workers=1,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        if not compiled_ok:
            raise RuntimeError("claudeglass.pyz build failed: compileall could not byte-compile the source tree")
        zipapp.create_archive(
            source=tmp_path,
            target=output,
            interpreter="/usr/bin/env python3",
            main="claudeglass.__main__:main",
            compressed=True,
        )

    return output


#: Run by :func:`probe` as ``python -I -c`` with the archive's path as its
#: argument. The archive goes first on ``sys.path``, so every import and
#: every file read below goes through ``zipimport`` and ``importlib.resources``
#: exactly as it does for ``python claudeglass.pyz``. The dashboard is
#: asked for its pages over HTTP, through ``make_handler``, so the code that
#: serves a static file is the code a user's dashboard runs.
_ARCHIVE_PROBE = r"""
import http.client, json, sys, tempfile, threading
from http.server import ThreadingHTTPServer
from pathlib import Path

archive = sys.argv[1]
sys.path.insert(0, archive)
import claudeglass
from claudeglass import cli
from claudeglass.profiles import catalogue
from claudeglass.service import api
from claudeglass.service.contracts import ServeOptions
from claudeglass.service.store import Store

found = {
    "zipimport": type(claudeglass.__spec__.loader).__name__ == "zipimporter",
    "subcommands": list(cli.SUBCOMMANDS),
    "profiles": [profile.id for profile in catalogue.list_profiles()],
    "pages": {},
}
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    store = Store(tmp / "service.db")
    store.open()
    options = ServeOptions(projects_root=tmp / "projects", config_dir=tmp / "config")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.make_handler(store, options))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path in sys.argv[2:]:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read()
            finally:
                conn.close()
            found["pages"][path] = {
                "status": response.status,
                "type": response.getheader("Content-Type"),
                "bytes": len(body),
                "placeholder": b"UI not built yet" in body,
            }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        store.close()
print(json.dumps(found))
"""

#: The dashboard's pages :func:`smoke` asks for, with the content type
#: each must come back as.
_EXPECTED_PAGES = {
    "/": "text/html",
    "/static/app.js": "text/javascript",
    "/static/app.css": "text/css; charset=utf-8",
}
#: Asked for too: each must be answered "not found".
_MISSING_PAGES = ("/static/does-not-exist.js", "/static/..%2f__init__.py")


def probe(pyz: Path) -> dict:
    """What the archive at ``pyz`` finds when it is the only copy of
    claudeglass on ``sys.path``: its subcommands, its shipped profile ids
    and how the dashboard answers for its pages (``_ARCHIVE_PROBE``).
    Raises ``RuntimeError`` when that run fails."""
    with tempfile.TemporaryDirectory(prefix="claudeglass-pyz-probe-") as tmp:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _ARCHIVE_PROBE, str(pyz.resolve()), *_EXPECTED_PAGES, *_MISSING_PAGES],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp,
        )
    if done.returncode != 0:
        raise RuntimeError(f"reading the archive failed: {(done.stderr.strip().splitlines() or [done.returncode])[-1]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def smoke(pyz: Path, *, every_subcommand: bool = True) -> list[str]:
    """What is wrong with the archive at ``pyz``, one line each; an empty
    list means it works. It must serve the dashboard's pages and list the
    shipped profiles (both read from inside the zip), answer ``--version``
    and, with ``every_subcommand``, print ``--help`` for each subcommand.
    That is what ``--version`` alone cannot show: a packaged file that is
    missing or a subcommand that fails to import fails here, not on a
    user's machine."""
    try:
        found = probe(pyz)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        return [str(exc)]
    problems: list[str] = []
    if not found["zipimport"]:
        problems.append("the package was not imported from the archive")
    if len(found["profiles"]) < 8:
        problems.append(f"only {len(found['profiles'])} shipped profiles were read")
    for path, content_type in _EXPECTED_PAGES.items():
        page = found["pages"].get(path, {})
        if page.get("status") != 200 or page.get("type") != content_type or not page.get("bytes"):
            problems.append(f"the dashboard did not serve {path}: {page}")
        elif page["placeholder"]:
            problems.append(f"the dashboard served its placeholder page for {path}")
    for path in _MISSING_PAGES:
        if found["pages"].get(path, {}).get("status") != 404:
            problems.append(f"the dashboard did not answer 404 for {path}")
    commands = [["--version"]]
    if every_subcommand:
        commands += [[name, "--help"] for name in found["subcommands"]]
    for command in commands:
        done = subprocess.run([sys.executable, str(pyz), *command], capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            problems.append(f"claudeglass.pyz {' '.join(command)} exited {done.returncode}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)}",
    )
    parser.add_argument(
        "--smoke",
        type=Path,
        metavar="PATH",
        help="build nothing: run the archive at PATH and report what does not work",
    )
    args = parser.parse_args(argv)

    if args.smoke is not None:
        problems = smoke(args.smoke)
        for problem in problems:
            print(f"FAILED: {problem}", file=sys.stderr)
        if not problems:
            print(f"{args.smoke} serves the dashboard, lists the profiles and prints every subcommand's help")
        return 1 if problems else 0

    output = build(args.output)
    print(f"built {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

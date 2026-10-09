"""Tests for ``scripts/build-pyz.py``'s compressed build and its
``--smoke`` check: the single-file archive is *run*, not just opened.

``tests/test_service_build_pyz.py`` covers what the archive holds and that
``--version`` works. This file covers what ``--version`` cannot show: with
the archive as the only copy of claudeglass, the dashboard must serve its
pages and the shipped profiles must load, both read from inside the zip. A
path built from ``__file__`` points into the archive and opens nothing, so
the first release of a build with one passed ``--version`` and served an
empty dashboard.

The archive is built once for the module; a build that cannot be made here
(no write access, no ``zlib``) skips these tests rather than failing them.
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

from claudeglass.profiles.catalogue import CATALOGUE_IDS

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-pyz.py"
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("_build_pyz_smoke", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_pyz():
    return _load_build_module()


@pytest.fixture(scope="module")
def built_pyz(build_pyz, tmp_path_factory) -> Path:
    pytest.importorskip("zlib")
    try:
        return build_pyz.build(tmp_path_factory.mktemp("pyz-smoke") / "claudeglass.pyz")
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"the archive cannot be built here: {exc}")


@pytest.fixture(scope="module")
def facts(build_pyz, built_pyz) -> dict:
    """What the archive finds, read in a subprocess where it is the only
    copy of claudeglass."""
    return build_pyz.probe(built_pyz)


def _without(source: Path, target: Path, *, drop: tuple[str, ...]) -> Path:
    """A copy of the archive ``source`` at ``target`` without the members
    whose names start with any of ``drop``."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if not item.filename.startswith(drop):
                dst.writestr(item, src.read(item.filename))
    return target


def test_the_archive_is_compressed(built_pyz: Path) -> None:
    with zipfile.ZipFile(built_pyz) as zf:
        members = [item for item in zf.infolist() if not item.is_dir()]
    assert members
    assert {item.compress_type for item in members} == {zipfile.ZIP_DEFLATED}
    assert built_pyz.stat().st_size < sum(item.file_size for item in members)


def test_the_archive_is_the_only_copy_the_probe_imports(facts: dict) -> None:
    assert facts["zipimport"] is True


def test_the_archive_lists_every_shipped_profile_from_inside_the_zip(facts: dict) -> None:
    assert facts["profiles"] == list(CATALOGUE_IDS)


def test_the_archive_serves_the_dashboard_from_inside_the_zip(facts: dict) -> None:
    pages = facts["pages"]
    index = pages["/"]
    assert index["status"] == 200
    assert index["type"] == "text/html"
    assert index["placeholder"] is False
    for path, content_type, name in (
        ("/static/app.js", "text/javascript", "app.js"),
        ("/static/app.css", "text/css; charset=utf-8", "app.css"),
    ):
        page = pages[path]
        assert page["status"] == 200, path
        assert page["type"] == content_type, path
        # The bytes the dashboard sends are the file's bytes, whole.
        assert page["bytes"] == (STATIC_DIR / name).stat().st_size, path


def test_the_archive_answers_not_found_for_a_missing_file_and_a_path_out_of_the_folder(build_pyz, facts: dict) -> None:
    for path in build_pyz._MISSING_PAGES:
        assert facts["pages"][path]["status"] == 404, path


def test_the_probe_knows_every_subcommand(facts: dict) -> None:
    assert {"report", "serve", "apply", "tuning"} <= set(facts["subcommands"])


def test_smoke_passes_on_the_built_archive(build_pyz, built_pyz: Path) -> None:
    # Every subcommand's --help runs in the release workflow; one run per
    # subcommand is too slow for the suite.
    assert build_pyz.smoke(built_pyz, every_subcommand=False) == []


def test_smoke_names_a_dashboard_that_cannot_find_its_files(build_pyz, built_pyz: Path, tmp_path: Path) -> None:
    broken = _without(built_pyz, tmp_path / "no-ui.pyz", drop=("claudeglass/service/static/",))
    problems = build_pyz.smoke(broken, every_subcommand=False)
    assert any("placeholder page for /" in line for line in problems), problems
    assert any("did not serve /static/app.js" in line for line in problems), problems
    assert any("did not serve /static/app.css" in line for line in problems), problems


def test_smoke_names_an_archive_that_cannot_read_its_profiles(build_pyz, built_pyz: Path, tmp_path: Path) -> None:
    broken = _without(built_pyz, tmp_path / "no-profiles.pyz", drop=("claudeglass/profiles/catalogue/",))
    problems = build_pyz.smoke(broken, every_subcommand=False)
    assert len(problems) == 1
    assert problems[0].startswith("reading the archive failed")


def test_the_smoke_flag_exits_one_and_prints_each_failure(
    build_pyz, built_pyz: Path, tmp_path: Path, capsys, monkeypatch
) -> None:
    run = build_pyz.smoke
    monkeypatch.setattr(build_pyz, "smoke", lambda path: run(path, every_subcommand=False))
    broken = _without(built_pyz, tmp_path / "no-ui.pyz", drop=("claudeglass/service/static/",))
    assert build_pyz.main(["--smoke", str(broken)]) == 1
    assert "FAILED: the dashboard did not serve /static/app.js" in capsys.readouterr().err
    assert build_pyz.main(["--smoke", str(built_pyz)]) == 0
    assert "serves the dashboard" in capsys.readouterr().out

"""The mod's plugin folder (``plugin/``) and the marketplace entry that points at it.

Claude Code reads ``plugin/.claude-plugin/plugin.json`` and
``plugin/hooks/hooks.json``; ``.claude-plugin/marketplace.json`` at the repo
root lets ``/plugin marketplace add`` find the plugin. These checks keep the
three in step with each other and with the Python package.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import claudeglass

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN = REPO_ROOT / "plugin"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
HOOKS = PLUGIN / "hooks" / "hooks.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_version_is_the_package_version():
    assert _load(MANIFEST)["version"] == claudeglass.__version__


def test_plugin_names_match():
    manifest = _load(MANIFEST)
    market = _load(MARKETPLACE)
    assert manifest["name"] == "claudeglass"
    assert market["name"] == "claudeglass"
    assert [entry["name"] for entry in market["plugins"]] == [manifest["name"]]


def test_marketplace_source_points_at_the_plugin_folder():
    market = _load(MARKETPLACE)
    for entry in market["plugins"]:
        source = entry["source"]
        assert source == "./plugin"
        assert (REPO_ROOT / source / ".claude-plugin" / "plugin.json").is_file()


def test_manifest_types_file_exists():
    types = _load(MANIFEST)["types"]
    assert (PLUGIN / types).is_file()


def test_hooks_json_names_modules_that_exist_and_export_register():
    modules = _load(HOOKS)["modules"]
    assert modules
    for name in modules:
        path = HOOKS.parent / name
        assert path.is_file(), name
        assert re.search(r"export const register\b", path.read_text(encoding="utf-8")), name


def test_every_hook_file_is_wired_into_the_register_file():
    """A new feature is one file plus one line in register.tsx: no file sits unreferenced."""
    hooks = PLUGIN / "hooks"
    register = (hooks / "register.tsx").read_text(encoding="utf-8")
    imported = "\n".join(path.read_text(encoding="utf-8") for path in hooks.glob("*.ts*") if path.name != "hooks.json")
    for path in sorted(hooks.glob("*.ts*")):
        stem = path.stem
        if stem == "register":
            continue
        assert re.search(rf"from '\./{re.escape(stem)}(\.js)?'", imported), f"{path.name} is imported by no hook file"
    assert "register" in register

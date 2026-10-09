"""The settings file the ClaudeGlass mod reads: ``<config_dir>/mod/settings.json``.

The mod runs inside Claude Code. It can't run Python or make an HTTP
call, so it reads this file with ``$.fs.read`` and imports the assets
``scripts/export-mod-assets.py`` generates. The file holds what only
ClaudeGlass knows on this machine:

- the billing mode and how a dollar reads in it (``report.meta.units``);
- the rate card in use, with context windows, and the cache lifetime;
- characters per token measured on your sessions (``calibration``);
- the coaching thresholds in force (the catalogue's, then yours from
  ``coaching.json`` and ``config.toml``, as the capture hook works them
  out), and the tips you muted or want once a session;
- "your usual": the median session cost and the median context a session
  ends on, from the dashboard's store;
- where the dashboard answers.

Privacy: numbers, the model ids the rate card names, and closed words.
No path, prompt or text from a transcript.

:func:`build` makes the dict, :func:`write` puts it on disk atomically and
leaves the file alone when only ``written_at`` would change. ``update
--finish`` writes it once, and the dashboard's service again after a
report build, at most hourly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, calibration as calibration_mod, statusline
from .config import _resolve_config_dir, load_config
from .hooks import capture_hook
from .pricing import Pricing, load_pricing
from .units import Units

#: Bump when a reader of the file would need to change.
SCHEMA = 1

#: The folder under the config dir, and the file in it.
MOD_DIRNAME = "mod"
SETTINGS_NAME = "settings.json"

#: Wildcard binds, which the dashboard answers on through loopback.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})

#: What the share of the usage limit is "of" (``report.meta.units``).
PERIOD_LABEL = "weekly usage limit"


def settings_path(config_dir: str | Path | None) -> Path:
    """``<config_dir>/mod/settings.json``; ``config_dir`` resolves as
    ``config.load_config`` resolves it."""
    return _resolve_config_dir(config_dir) / MOD_DIRNAME / SETTINGS_NAME


def dashboard_url(bind: str, port: int) -> str | None:
    """Where the dashboard answers for ``serve``'s ``bind`` and ``port``
    (loopback for a wildcard bind), or ``None`` for no real port."""
    if not port:
        return None
    host = "127.0.0.1" if bind in _WILDCARD_BINDS else bind
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/"


def pricing_block(pricing: Pricing) -> dict:
    """The rate card as the mod reads it: the version, and for each
    canonical model id its aliases, the five per-million-token rates and
    the context window."""
    aliases: dict[str, list[str]] = {}
    for alias, canonical in pricing.aliases.items():
        aliases.setdefault(canonical, []).append(alias)
    models = {}
    for model_id, rates in pricing.models.items():
        models[model_id] = {
            "aliases": sorted(aliases.get(model_id, [])),
            "input": float(rates.input),
            "output": float(rates.output),
            "cache_write_5m": float(rates.cache_write_5m),
            "cache_write_1h": float(rates.cache_write_1h),
            "cache_read": float(rates.cache_read),
            "context_window_tokens": int(rates.context_window_tokens),
        }
    return {"version": pricing.version, "models": models}


def _coaching_block(config_dir: Path) -> dict:
    """The thresholds, muted tips and once-a-session tips the capture
    hook would use, worked out by the hook's own functions."""
    coaching = capture_hook.load_catalogue()["coaching"]
    try:
        config = capture_hook.load_config(config_dir)
    except ValueError:  # tomllib.TOMLDecodeError: load_config already reported it
        config = {}
    personal = capture_hook._read_json(config_dir / coaching["file"])
    return {
        "thresholds": capture_hook.coaching_thresholds(coaching, config, personal),
        "muted": sorted(capture_hook._hint_names(personal.get("muted"))),
        "once": sorted(capture_hook._hint_names(personal.get("once"))),
    }


def _usual_block(store) -> dict:
    """Your median session cost and median context at a session's end,
    from the store; nothing when there is no store or it can't answer."""
    cost = context = None
    if store is not None:
        try:
            cost = store.median_session_cost()
            context = store.median_end_context()
        except sqlite3.Error:
            cost = context = None
    return {
        "session_cost_usd": None if cost is None else float(cost),
        "end_context_tokens": None if context is None else int(context),
    }


def build(
    config_dir: str | Path | None,
    *,
    report=None,
    store=None,
    dashboard_url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """The settings file's content. ``report`` (a built ``ReportModel``)
    adds the share of the usage limit a dollar is worth and the measured
    characters per token; without one they stay unknown and the fallbacks
    stand in. ``store`` adds "your usual". ``dashboard_url`` is where the
    dashboard answers, when known. Raises ``ConfigError`` or
    ``PricingError`` for a config or rate card that can't be read."""
    config_dir = _resolve_config_dir(config_dir)
    config = load_config(config_dir)
    pricing = load_pricing(path=config.pricing_path, config_dir=config_dir)
    units = report.meta.units if report is not None else {}
    share = units.get("share_per_usd")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema": SCHEMA,
        "written_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": __version__,
        "billing": {
            "mode": config.billing,
            "share_per_usd": None if share is None else float(share),
            "period_label": PERIOD_LABEL,
            "basis": units.get("basis") or Units(billing_mode=config.billing).basis(),
        },
        "currency": pricing.currency,
        "pricing": pricing_block(pricing),
        "cache": {"ttl_seconds": statusline.resolve_effective_ttl({}, config_dir)},
        "calibration": (report.meta.calibration if report is not None else None) or calibration_mod.DEFAULT.to_dict(),
        "coaching": _coaching_block(config_dir),
        "usual": _usual_block(store),
        "dashboard_url": dashboard_url,
    }


def _same_but_for_time(old: object, new: dict) -> bool:
    return isinstance(old, dict) and {**old, "written_at": None} == {**new, "written_at": None}


def write(config_dir: str | Path | None, data: dict) -> bool:
    """Write ``data`` to the settings file through a temporary file and a
    rename, so the mod never reads half a file. Creates ``mod/``. Leaves
    the file alone when it already holds ``data`` apart from
    ``written_at``. Returns whether it wrote; an ``OSError`` gives
    ``False``."""
    path = settings_path(config_dir)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if _same_but_for_time(existing, data):
        return False
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


__all__ = ["SCHEMA", "build", "dashboard_url", "pricing_block", "settings_path", "write"]

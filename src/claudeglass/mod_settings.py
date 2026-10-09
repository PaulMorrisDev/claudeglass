"""The settings file the ClaudeGlass mod reads: ``<config_dir>/mod/settings.json``.

The mod runs inside Claude Code. It can't run Python or make an HTTP
call, so it reads this file with ``$.fs.read`` and imports the assets
``scripts/export-mod-assets.py`` generates. The file holds what only
ClaudeGlass knows on this machine:

- the billing mode and how a dollar reads in it (``report.meta.units``);
- the rate card in use, with context windows (and any long-context rule),
  and the cache lifetime, with where it came from: ``config.toml``, your
  history of cache writes, or the five-minute default;
- characters per token measured on your sessions (``calibration``);
- the thresholds a cache rebuild is judged by (``rebuild``);
- the coaching thresholds in force (the catalogue's, then yours from
  ``coaching.json`` and ``config.toml``, as the capture hook works them
  out), the tips you muted or want once a session, and whether coaching is
  on at all with the project filter the hook applies, so the mod coaches
  only where the hook would;
- "your usual": the median session cost and the median context a session
  ends on, from the dashboard's store;
- where the dashboard answers.

Privacy: numbers, the model ids the rate card names, and closed words.
No path, prompt or text from a transcript. Two things in ``coaching`` are
mirrors of your own config rather than exports: the project slug patterns
of ``[capture] projects`` and ``exclude_projects`` (as you wrote them), and
the ids of sessions you rated lately (the ids the mod's heartbeat files are
named by).

:func:`build` makes the dict, :func:`write` puts it on disk atomically and
leaves the file alone when only ``written_at`` would change, unless that
is a day old. ``update --finish`` writes it once, carrying forward what only
a report knows from the file the service wrote (``previous``), and the
dashboard's service again after a report build, at most hourly, and
:func:`refresh_coaching` brings just the coaching block up to date when a
rating or a capture setting changes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, calibration as calibration_mod, recache, statusline
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

#: How far back the cache writes that tell the cache lifetime are counted.
CACHE_HISTORY_DAYS = 14

#: The cache lifetime when your writes were mostly one-hour ones, in seconds.
ONE_HOUR_TTL_S = 3600

#: How far back the dashboard ratings the settings file lists reach, in seconds.
RATED_WINDOW_S = 30 * 86400

#: A file holding the same content is rewritten once its ``written_at`` is
#: this old, so the stamp says when the file was last known to be right.
STALE_AFTER = timedelta(hours=24)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Held while the file is written, and across :func:`refresh_coaching`'s
#: read-and-write, so two writers in this process (the hourly report's
#: thread and a rating's refresh) never interleave. Re-entrant: the refresh
#: writes through :func:`write` while holding it.
_WRITE_LOCK = threading.RLock()


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
    canonical model id its aliases, the five per-million-token rates, the
    context window and, for a model that has one, its ``long_context``
    rule. An alias is listed once, under the model ``Pricing.resolve_model``
    gives it (the last to name it), so the order the file's JSON is read in
    can't change which model it means; an alias that is also a model's id
    is that model's, and isn't listed."""
    aliases: dict[str, list[str]] = {}
    for alias, canonical in pricing.aliases.items():
        if alias not in pricing.models:
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
        if rates.long_context is not None:
            models[model_id]["long_context"] = _long_context(rates.long_context)
    return {"version": pricing.version, "models": models}


def _long_context(rule) -> dict:
    out: dict = {"threshold_tokens": int(rule.threshold_tokens)}
    if rule.multiplier is not None:
        out["multiplier"] = float(rule.multiplier)
    if rule.overrides:
        out["overrides"] = {key: float(value) for key, value in rule.overrides.items()}
    return out


def _cache_block(config_dir: Path, store, stamp: datetime) -> dict:
    """The cache lifetime the mod counts down from, and where it came from:
    ``config.toml``'s ``default_ttl`` ("config"); else one hour when, over
    the last :data:`CACHE_HISTORY_DAYS` days, more of your top-level cache
    writes were one-hour than five-minute, five minutes when not
    ("history"); else the five-minute default ("default")."""
    configured = statusline._read_default_ttl_from_config(config_dir)
    if configured is not None:
        return {"ttl_seconds": configured, "source": "config"}
    if store is not None:
        try:
            five_minute, one_hour = store.cache_write_split(CACHE_HISTORY_DAYS, stamp)
        except sqlite3.Error:
            five_minute = one_hour = 0
        if five_minute or one_hour:
            return {"ttl_seconds": ONE_HOUR_TTL_S if one_hour > five_minute else 300, "source": "history"}
    return {"ttl_seconds": statusline.resolve_effective_ttl({}, config_dir), "source": "default"}


def _pattern_list(value) -> list[str]:
    """The slug patterns of ``[capture] projects`` or ``exclude_projects``,
    as the user wrote them (strings only); nothing for anything else."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _coaching_block(config_dir: Path, now: datetime) -> dict:
    """What the capture hook's coaching would use, worked out by the hook's
    own functions: whether it is on at all (``coaching_notes`` in ``[capture]
    coaching``), the project filter it applies, the thresholds, the muted and
    once-a-session tips, the plan tip's opt-out, and what the rating reminder
    needs (``feedback_reminder`` opted in, when the hook last reminded, and the
    sessions rated on the dashboard lately).

    ``projects`` and ``exclude_projects`` mirror the user's own config as
    written (slug patterns): this file is a local mirror of that config, not
    an export, and the mod applies the same filter to the project it runs in.
    ``rated`` maps session ids (the ones the mod names its heartbeat files
    by) to when the rating was given, for the last 30 days."""
    coaching = capture_hook.load_catalogue()["coaching"]
    try:
        config = capture_hook.load_config(config_dir)
    except ValueError:  # tomllib.TOMLDecodeError: load_config already reported it
        config = {}
    personal = capture_hook._read_json(config_dir / coaching["file"])
    capture = config.get("capture") if isinstance(config.get("capture"), dict) else {}
    state = capture_hook._read_json(config_dir / coaching["state_file"])
    gate = state.get("feedback") if isinstance(state.get("feedback"), dict) else {}
    return {
        "enabled": capture_hook.coaching_on(config),
        "projects": _pattern_list(capture.get("projects")),
        "exclude_projects": _pattern_list(config.get("exclude_projects")),
        "feedback_reminder": "feedback_reminder" in capture_hook.feedback_ids(config),
        # ``coaching.json``'s ``plan_fresh: false`` switches the plan tip off.
        "plan_fresh": personal.get("plan_fresh", True) is not False,
        "thresholds": capture_hook.coaching_thresholds(coaching, config, personal),
        "muted": sorted(capture_hook._hint_names(personal.get("muted"))),
        "once": sorted(capture_hook._hint_names(personal.get("once"))),
        # What the rating reminder compares a piece of work with (``coaching.json``).
        "typical_piece_tokens": int(capture_hook._number(personal.get("typical_piece_tokens")) or 0),
        # When the hook last gave the rating reminder (epoch seconds).
        "reminded_at": capture_hook._number(gate.get("reminded_at")),
        "rated": {
            sid: round(when)
            for sid, when in capture_hook.dashboard_ratings(config_dir, now.timestamp() - RATED_WINDOW_S).items()
        },
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
    previous: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """The settings file's content. ``report`` (a built ``ReportModel``)
    adds the share of the usage limit a dollar is worth and the measured
    characters per token; without one they stay unknown and the fallbacks
    stand in, unless ``previous`` (the file as it was) has them: then its
    ``calibration`` and ``dashboard_url`` carry forward, and its share and
    basis too when its billing mode is still this one. ``store`` adds "your
    usual" and the cache lifetime from your history. ``dashboard_url`` is
    where the dashboard answers, when known. Raises ``ConfigError`` or
    ``PricingError`` for a config or rate card that can't be read."""
    config_dir = _resolve_config_dir(config_dir)
    config = load_config(config_dir)
    pricing = load_pricing(path=config.pricing_path, config_dir=config_dir)
    units = report.meta.units if report is not None else {}
    share = units.get("share_per_usd")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    billing = {
        "mode": config.billing,
        "share_per_usd": None if share is None else float(share),
        "period_label": PERIOD_LABEL,
        "basis": units.get("basis") or Units(billing_mode=config.billing).basis(),
    }
    measured = report.meta.calibration if report is not None else None
    if report is None and isinstance(previous, dict):
        old_billing = previous.get("billing")
        if isinstance(old_billing, dict) and old_billing.get("mode") == config.billing:
            old_share = old_billing.get("share_per_usd")
            if isinstance(old_share, (int, float)) and not isinstance(old_share, bool):
                billing["share_per_usd"] = float(old_share)
            if isinstance(old_billing.get("basis"), str) and old_billing["basis"]:
                billing["basis"] = old_billing["basis"]
        if isinstance(previous.get("calibration"), dict) and previous["calibration"]:
            measured = previous["calibration"]
        if isinstance(previous.get("dashboard_url"), str) and previous["dashboard_url"]:
            dashboard_url = previous["dashboard_url"]
    return {
        "schema": SCHEMA,
        "written_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": __version__,
        "billing": billing,
        "currency": pricing.currency,
        "pricing": pricing_block(pricing),
        "cache": _cache_block(config_dir, store, stamp),
        "rebuild": {
            **dataclasses.asdict(recache.RecacheThresholds.from_config(config.thresholds)),
            "session_read_margin": recache.SESSION_READ_MARGIN,
        },
        "calibration": measured or calibration_mod.DEFAULT.to_dict(),
        "coaching": _coaching_block(config_dir, stamp),
        "usual": _usual_block(store),
        "dashboard_url": dashboard_url,
    }


def _stamp(value: object) -> datetime | None:
    try:
        return datetime.strptime(value, _STAMP_FORMAT).replace(tzinfo=timezone.utc)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _same_but_for_time(old: object, new: dict) -> bool:
    """Whether ``old`` holds ``new``'s content and a ``written_at`` no more
    than :data:`STALE_AFTER` before ``new``'s (a stamp that can't be read
    is not the same)."""
    if not isinstance(old, dict) or {**old, "written_at": None} != {**new, "written_at": None}:
        return False
    was, now = _stamp(old.get("written_at")), _stamp(new.get("written_at"))
    return was is not None and now is not None and now - was <= STALE_AFTER


def write(config_dir: str | Path | None, data: dict, *, recompute_coaching: bool = True) -> bool:
    """Write ``data`` to the settings file through a temporary file and a
    rename, so the mod never reads half a file. Creates ``mod/``. Leaves
    the file alone when it already holds ``data`` apart from
    ``written_at``, unless that is more than a day old. Returns whether it wrote; an ``OSError`` gives
    ``False``.

    One writer at a time (:data:`_WRITE_LOCK`), each through a temporary
    file of its own. The ``coaching`` block of ``data`` is worked out again
    inside the lock, just before the write, unless ``recompute_coaching`` is
    false: a report takes minutes to build, and a rating given in the
    meantime must not be written over with the block as it stood when the
    build began. (The block is reckoned as of ``data``'s ``written_at``.)"""
    path = settings_path(config_dir)
    with _WRITE_LOCK:
        if recompute_coaching and isinstance(data.get("coaching"), dict):
            try:
                data = {**data, "coaching": _coaching_block(path.parent.parent, _stamp(data.get("written_at")) or datetime.now(timezone.utc))}
            except Exception:  # noqa: BLE001 -- the block as built stands
                pass
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if _same_but_for_time(existing, data):
            return False
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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


def refresh_coaching(config_dir: str | Path | None, *, now: datetime | None = None) -> bool:
    """Bring the ``coaching`` block of the file up to date now, leaving the
    rest of it as it is. A rating given on the dashboard or a change to
    ``[capture]`` moves what the block says (the ratings, whether coaching
    and the rating reminder are on, the project filter), and the mod
    should not wait for the next report to hear of it. Cheap: no report and
    no store. Nothing is written when there is no file yet (the next report
    writes it whole) or it can't be read. Never raises; returns whether it
    wrote."""
    try:
        directory = _resolve_config_dir(config_dir)
        with _WRITE_LOCK:  # the read, the new block and the write are one step
            existing = json.loads(settings_path(directory).read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or existing.get("schema") != SCHEMA:
                return False
            stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            data = {**existing, "coaching": _coaching_block(directory, stamp), "written_at": stamp.strftime(_STAMP_FORMAT)}
            return write(directory, data, recompute_coaching=False)
    except Exception:  # noqa: BLE001 -- a courtesy to the mod: the next report writes the whole file
        return False


__all__ = ["SCHEMA", "build", "dashboard_url", "pricing_block", "refresh_coaching", "settings_path", "write"]

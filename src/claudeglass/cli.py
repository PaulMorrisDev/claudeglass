"""Command-line interface for claudeglass.

WP0 shipped the argparse surface and subcommand stubs. WP10c (this
module) wires every subcommand up to the engine WP1-WP9/WP10a already
built: ``report``/``sessions``/``recache``/``ttl``/``compactions`` all
go through :func:`~claudeglass.report.build_report`, filtered to
a single section (plus ``overview``) for the four focused subcommands;
``config-diff`` is its own consumer of
:mod:`~claudeglass.snapshots` (see :func:`_build_session_metrics`'s
docstring for why it can't just reuse ``build_report``'s "config"
section); ``log-usage``, ``probe``, ``statusline`` and ``scrub-fixture``
delegate to their own modules; ``pricing-check`` and ``snapshot-config``
are unchanged from WP2/WP7.

Exit codes throughout: 0 ok, 1 no data (an empty corpus, or no config
snapshots for ``config-diff`` -- always with a one-line reason on
stderr naming the projects root and window), 2 bad input (a
``ConfigError``/``PricingError``, a bad flag combination argparse
itself doesn't already catch, or an unimplemented subcommand).
"""

from __future__ import annotations

import argparse
import importlib.resources
import importlib.util
import json
import os
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import available_timezones

from . import __version__, baseline as baseline_mod, capture_catalogue, capture_view, classify, discovery, installer as installer_mod
from . import invocation, onboarding, setup_flow
from . import pages
from . import helptext, hook_health, probe as probe_mod, recache, signals as signals_mod, snapshots
from . import statusline as statusline_mod
from .fixes import RESTART_NOTE
from .cache import DigestCache
from .capture import HISTORY_DAYS as CAPTURE_HISTORY_DAYS
from .config import (
    CAPTURE_SAMPLES,
    RETENTION_DAYS_MAX,
    RETENTION_DAYS_MIN,
    SIGNAL_RETENTION_DEFAULT_DAYS,
    CaptureConfig,
    Config,
    ConfigError,
    feedback_ids,
    load_config,
    load_session_overrides,
    prune_capture_log,
    set_capture,
)
from .corpus import Corpus, load_corpus
from .parse import load_or_create_salt
from .model import Diagnostics, EventKind, PricingMeta, ReportMeta, ReportModel, Section, TranscriptResult
from .pricing import Pricing, PricingCoverage, PricingError, load_pricing, price_turn
from .render.csv_out import write_csv_dir
from .render.html import render_html
from .render.json_out import render_json
from .render.markdown import render_markdown
from .render.tables import format_cell
from .report import build_report
from .scorecard import ScorecardError
from .tools import log_usage as log_usage_mod
from .tools import scrub as scrub_mod

#: Every subcommand in the CLI surface, in the order they are
#: registered. "report" is also the default when no subcommand is given.
SUBCOMMANDS: tuple[str, ...] = (
    "report",
    "sessions",
    "recache",
    "ttl",
    "limits",
    "carry",
    "compaction-sim",
    "model-swap",
    "waste",
    "quality",
    "compactions",
    "config-diff",
    "snapshot-config",
    "probe-config",
    "log-usage",
    "pricing-check",
    "scrub-fixture",
    "probe",
    "statusline",
    "export",
    "monthly-report",
    "compare",
    "reconcile",
    "init",
    "baseline",
    "apply",
    "serve",
    "install-service",
    "uninstall-service",
    "update",
    "changes",
    "status",
    "review",
    "check",
    "uninstall",
    "import",
    "team-report",
    "capture",
    "backtest",
)

DEFAULT_SUBCOMMAND = "report"

# Tokens that must never trigger default-subcommand insertion because
# argparse needs to see them as the very first token.
_LEADING_PASSTHROUGH = ("-h", "--help", "--version")

#: Subcommands whose output is a filtered :func:`report.build_report`
#: section (plus "overview"), keyed by the section key they add. "report"
#: itself passes ``include=None`` (the whole report), so isn't listed
#: here.
_REPORT_LIKE_SECTIONS: dict[str, str] = {
    "sessions": "sessions",
    "recache": "recache",
    "ttl": "ttl",
    "limits": "limits",
    "carry": "carry",
    "compaction-sim": "compaction_sim",
    "model-swap": "model_swap",
    "waste": "waste",
    "quality": "quality",
    "compactions": "compactions",
}

#: Subcommands that render a report (directly or via a filtered
#: section) and so accept the shared output flags.
_REPORT_LIKE_COMMANDS: tuple[str, ...] = ("report", *_REPORT_LIKE_SECTIONS)


def _build_common_parser() -> argparse.ArgumentParser:
    """Global options shared by every subcommand, per the plan's CLI
    surface. Returned as a parent parser so each subcommand inherits them.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--projects-root",
        action="append",
        default=None,
        help="folder of Claude Code project folders; repeatable. Default ~/.claude/projects, "
        "plus any extra_projects_roots in config.toml (such as a WSL distro's)",
    )
    common.add_argument(
        "--project",
        action="append",
        default=None,
        help="repeatable; default is the current directory's project slug",
    )
    common.add_argument("--all-projects", action="store_true")
    common.add_argument("--project-family", metavar="REGEX")

    window = common.add_mutually_exclusive_group()
    window.add_argument("--days", type=int)
    window.add_argument("--since")
    common.add_argument("--until")
    common.add_argument("--limit", type=int)

    common.add_argument(
        "--window-by", choices=("last-reply", "mtime", "timestamp"), default="last-reply"
    )
    common.add_argument("--pricing", metavar="PATH")
    common.add_argument(
        "--config-dir", metavar="PATH", default=None, help="default: ~/.claude/claudeglass"
    )
    common.add_argument(
        "--tz",
        metavar="ZONE",
        default=None,
        help="IANA zone name overriding config.toml's tz for this run only "
        "(e.g. America/New_York); default: config.toml's tz, or the "
        "machine's own local zone",
    )
    common.add_argument(
        "--group-by",
        # Fix R6: this tuple used to be hand-maintained and had drifted
        # from classify._GROUP_KEYS (it offered a non-existent
        # "profile" key -- which classify.group_sessions() would reject
        # with an uncaught ValueError deep inside build_report() rather
        # than a clean CLI error -- and omitted the real "entrypoint"
        # key entirely). Derive the choices so they can never drift
        # again; argparse itself exits 2 with a one-line message on an
        # invalid choice.
        choices=sorted(classify._GROUP_KEYS),
    )

    cache = common.add_mutually_exclusive_group()
    cache.add_argument("--no-cache", action="store_true")
    cache.add_argument("--rebuild-cache", action="store_true")

    common.add_argument(
        "--jobs", type=int, default=1, metavar="N", help="parallel parsing workers (default: 1)"
    )

    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true")
    verbosity.add_argument("--verbose", action="store_true")

    return common


def _add_report_output_args(sub: argparse.ArgumentParser, *, allow_patch_set: bool = False) -> None:
    """Flags shared by every report-like subcommand (``report`` and the
    focused views: ``sessions``, ``recache``, ``ttl``, ``compactions``,
    ``limits``, ``carry``, ``compaction-sim``, ``model-swap``, ``waste``).
    """
    sub.add_argument(
        "--json", action="store_true", help="print the whole report as JSON instead of Markdown"
    )
    sub.add_argument(
        "--html", metavar="PATH", help="also write a single-file HTML report to PATH"
    )
    sub.add_argument(
        "--explain",
        action="store_true",
        help="add what each table shows, how to read it and when to act (Markdown output)",
    )
    sub.add_argument(
        "--csv-dir", metavar="DIR", help="also write one CSV file per table (plus an index) to DIR"
    )
    sub.add_argument(
        "--phases",
        action="store_true",
        help="add the DISCOVERY/IMPLEMENTATION/VERIFICATION phase-split section",
    )
    # v0.3 Task 2: --baseline is deliberately on _add_report_output_args
    # itself (not gated behind a keyword-only flag the way --patch-set
    # is via allow_patch_set) so every report-like subcommand
    # (report/sessions/recache/ttl/compactions) accepts it uniformly --
    # see report.build_report's own docstring for why the resulting
    # baseline_comparison section bypasses --group-by-style include
    # filtering rather than silently vanishing on a focused subcommand.
    sub.add_argument(
        "--baseline",
        metavar="ID|latest",
        help="add a baseline_comparison section against a saved `baseline` record",
    )
    # Fix R17: --allow-titles was removed -- report.py's own module
    # docstring documents that its allow_titles parameter is a
    # currently-permanent no-op (nothing anywhere in this codebase
    # captures customTitle/ai-title text to gate in the first place), so
    # the flag implied a privacy control that did not actually exist.
    # build_report() still accepts the keyword (matching its required
    # signature; report.py is out of this fix's file scope), always
    # called with the default.
    if allow_patch_set:
        sub.add_argument(
            "--patch-set",
            action="store_true",
            help="print the recommendation patch set after the report",
        )


def _add_config_diff_args(sub: argparse.ArgumentParser) -> None:
    group = sub.add_mutually_exclusive_group(required=True)
    group.add_argument("--key", metavar="KEY", help="one flattened config key to diff sessions by")
    group.add_argument(
        "--auto-keys",
        action="store_true",
        help="diff every config key that changed across the available snapshots",
    )


def _add_compare_output_args(sub: argparse.ArgumentParser) -> None:
    """``--json``/``--html``/``--csv-dir`` only -- ``compare`` has no
    ``--phases``/``--patch-set`` concept, so it doesn't share
    ``_add_report_output_args`` wholesale.
    """
    sub.add_argument(
        "--json", action="store_true", help="print the comparison as JSON instead of Markdown"
    )
    sub.add_argument(
        "--html", metavar="PATH", help="also write a single-file HTML comparison to PATH"
    )
    sub.add_argument(
        "--csv-dir", metavar="DIR", help="also write one CSV file per table (plus an index) to DIR"
    )


def _add_compare_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--a",
        required=True,
        metavar="SPEC",
        dest="arm_a",
        help="arm A selector: window:<since>..<until>, key:<key>=<value>, profile:<id>, or project:<slug>[,<slug>...]",
    )
    sub.add_argument(
        "--b",
        required=True,
        metavar="SPEC",
        dest="arm_b",
        help="arm B selector, same grammar as --a",
    )
    sub.add_argument(
        "--stratify",
        default=None,
        metavar="KEY,KEY",
        help="comma-separated stratification keys (purpose, mode, task); default: purpose,mode, plus task "
        "(the kind of task metrics capture reported) once half of both arms' sessions have one",
    )
    sub.add_argument(
        "--min-sessions",
        type=int,
        default=None,
        metavar="N",
        help="minimum sessions required per arm (per stratum) before a row counts as sample_ok "
        "(default: config.toml's min_sessions, itself 5)",
    )
    _add_compare_output_args(sub)


def _add_reconcile_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--admin-csv", required=True, metavar="FILE", help="path to an Admin API usage/cost export CSV"
    )
    sub.add_argument(
        "--by",
        default="day",
        choices=("day", "model", "day,model"),
        help="grouping for the reconciliation table (default: day)",
    )
    _add_compare_output_args(sub)


def _add_scrub_fixture_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--session-dir", metavar="PATH", help="path to <project_dir>/<session_id> to scrub")
    sub.add_argument("--out", metavar="PATH", help="output directory to write the scrubbed session into")
    sub.add_argument("--verify", metavar="OUT_DIR", help="run the privacy scan over an already-scrubbed directory")
    sub.add_argument(
        "--key-seed",
        default=None,
        metavar="SEED",
        help="deterministic HMAC key seed (tests only) -- omit for a random key",
    )


def _add_probe_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--file", metavar="PATH", help="probe a single transcript file instead of a project"
    )


def _add_statusline_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--print-install-fragment",
        "--install",
        dest="print_install_fragment",
        action="store_true",
        help="print the statusLine settings.json fragment instead of reading stdin",
    )


def _add_export_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``export`` subcommand (S1-exports, plan "Feeds
    existing tooling" / "Aggregation without surveillance"): aggregate-only
    and hashed by default (in every mode -- fix for review finding 2),
    per-session is opt-in and the hashing opt-out still redacts the
    OS-username segment rather than printing the slug fully raw (see
    ``exports._apply_slug_redaction``).
    """
    sub.add_argument(
        "--format",
        choices=("csv-flat", "json", "otel-jsonl"),
        default="csv-flat",
        help="export format (default: csv-flat)",
    )
    aggregate = sub.add_mutually_exclusive_group()
    aggregate.add_argument(
        "--aggregate-only",
        action="store_true",
        dest="aggregate_only",
        default=None,
        help="no session ids, no per-session rows (default)",
    )
    aggregate.add_argument(
        "--per-session",
        action="store_false",
        dest="aggregate_only",
        help="opt in to per-session rows (includes session ids)",
    )
    hash_slugs = sub.add_mutually_exclusive_group()
    hash_slugs.add_argument(
        "--hash-slugs",
        action="store_true",
        dest="hash_slugs",
        default=None,
        help="replace project slugs with a salted hash (default, in every mode)",
    )
    hash_slugs.add_argument(
        "--no-hash-slugs",
        action="store_false",
        dest="hash_slugs",
        help="don't hash project slugs -- an explicit, informed opt-out, not the "
        "default; the OS-username segment is still redacted to '<user>' "
        "rather than printed raw, and a one-line warning is printed to stderr",
    )
    sub.add_argument("--out", metavar="PATH", help="write to PATH instead of stdout")
    sub.add_argument(
        "--generated-at",
        metavar="ISO8601",
        default=None,
        dest="generated_at",
        help="override --format json's meta.generated_at (also honours the "
        "SOURCE_DATE_EPOCH env var) so the export is byte-reproducible "
        "(nit 21/19: it wasn't wired up to the CLI before)",
    )
    # v0.3 Task 1: a team-aggregate document is a different shape from
    # every other --format (per-group sums, never a session id or a
    # per-session row), so it's its own flag rather than a --format
    # choice -- --aggregate-only/--per-session/--hash-slugs (which
    # govern the *per-session* export shapes) have no effect on it, and
    # --format itself is ignored (a team document is always JSON) since
    # it has no CSV/OTel analogue.
    sub.add_argument(
        "--aggregate",
        action="store_true",
        help="write a team-aggregate JSON document (see `import`/`team-report`, "
        "docs/team.md) instead of a per-project export; aggregate-only and "
        "hashed by construction",
    )
    sub.add_argument(
        "--include-projects",
        action="store_true",
        dest="include_projects",
        help="with --aggregate, add a 'projects' list of hashed project slugs "
        "(default: omitted -- opt in per person)",
    )


def _add_monthly_report_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``monthly-report`` subcommand (S1-exports, plan
    "Finance" / feature 10 promoted to v0.2 ``serve --monthly-report``).
    """
    sub.add_argument(
        "--out", metavar="DIR", required=True, help="directory to write the Markdown/HTML report into"
    )
    sub.add_argument(
        "--month",
        metavar="YYYY-MM",
        default=None,
        help="calendar month to report on (default: the previous calendar month)",
    )
    sub.add_argument(
        "--generated-at",
        metavar="ISO8601",
        default=None,
        dest="generated_at",
        help="override the report's trailing 'Generated at: ...' line/comment "
        "(also honours the SOURCE_DATE_EPOCH env var) so repeated runs are "
        "genuinely byte-identical (fix for review finding 11)",
    )


def _add_serve_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for the ``serve`` subcommand (v0.2's local JSON API +
    watcher service, ``service/serve.py``). ``--projects-root`` and
    ``--config-dir`` are already on the common parser; everything below
    is serve-only.
    """
    sub.add_argument("--port", type=int, default=8765, help="default: 8765")
    sub.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="default: 127.0.0.1 (loopback only)",
    )
    sub.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow --bind to a non-loopback address (refused by default)",
    )
    sub.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        metavar="NAME",
        dest="allowed_host",
        help="repeatable; an extra host name the browser may use to reach the dashboard "
        "(loopback names and a specific --bind address are always allowed)",
    )
    sub.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        metavar="SECONDS",
        dest="poll_interval",
        help="watcher poll interval in seconds (default: 30)",
    )
    sub.add_argument(
        "--retention-days",
        type=int,
        default=None,
        metavar="N",
        help="prune sessions older than N days on every poll tick (default: config.toml's retention_days, else keep forever)",
    )
    sub.add_argument(
        "--exclude-project",
        action="append",
        default=None,
        metavar="SLUG",
        dest="exclude_project",
        help="repeatable; project slug never scanned",
    )
    sub.add_argument(
        "--once",
        action="store_true",
        help="run a single watcher tick, print its WatcherStats, and exit instead of serving",
    )
    sub.add_argument(
        "--exit-on-code-change",
        action="store_true",
        dest="exit_on_code_change",
        help="exit once claudeglass's own files change on disk (an update without a restart), "
        "so the service manager starts serve again on the new code; install-service sets it",
    )
    sub.add_argument(
        "--billing-mode",
        choices=("api", "subscription"),
        default=None,
        dest="billing_mode",
        metavar="{api,subscription}",
        help="stamped onto every session (default: 'billing' from "
        "<config-dir>/config.toml, else worked out from your transcripts)",
    )
    sub.add_argument(
        "--monthly-report",
        default=None,
        metavar="DIR",
        dest="monthly_report_dir",
        help="while serving, write the previous month's report (as 'monthly-report' does) into DIR "
        "when it is missing; checked at startup and hourly (default: none)",
    )
    sub.add_argument(
        "--store",
        default=None,
        metavar="PATH",
        dest="store_path",
        help="the dashboard's database file (default: <config-dir>/service.db); give a second "
        "serve its own so it never shares one with the service",
    )
    sub.add_argument(
        "--purge",
        action="store_true",
        help="delete the dashboard's database (<config-dir>/service.db, or --store) and its "
        "WAL/SHM sidecars, then exit; requires --yes",
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        help="confirm a destructive flag such as --purge (no interactive prompt)",
    )


def _add_install_service_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``install-service`` (v3): register ``serve`` to
    start at logon/boot. ``--projects-root``/``--config-dir`` are
    already on the common parser.
    """
    sub.add_argument("--port", type=int, default=8765, help="default: 8765 (must match how you run 'serve')")
    sub.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="default: 127.0.0.1 (loopback only)",
    )
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print exactly what would be written/run, without writing or running anything",
    )


def _add_update_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``update``: where to install from, the same
    ``--port``/``--bind``/``--dry-run`` as ``install-service``, which it
    runs to restart the dashboard, and ``--claude-root`` for the
    settings.json entries it brings up to date."""
    _add_install_service_args(sub)
    _add_claude_root_arg(sub)
    sub.add_argument(
        "--from",
        dest="source",
        default=UPDATE_SOURCE,
        metavar="SOURCE",
        help="what pip installs from (default: the GitHub repository; a local folder also works)",
    )
    sub.add_argument(
        "--no-service",
        action="store_true",
        dest="no_service",
        help="install the new version but leave the running dashboard alone",
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        dest="yes",
        help="answer yes to each change it offers (settings.json entries, stopping an old dashboard, "
        "removing copies for other Pythons)",
    )
    sub.add_argument(
        "--finish",
        action="store_true",
        help="finish an update without installing: restart the dashboard on this version, bring hook entries "
        "up to date and look for copies installed for other Pythons. update runs this itself; run it by hand "
        "after updating from 0.6.0 or older",
    )


def _add_uninstall_service_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``uninstall-service`` (v3): the inverse of
    ``install-service``. Takes no ``--port``/``--bind`` -- removing a
    registration never depends on them (see
    ``installer.InstallPlan.uninstall_commands``/``uninstall_files``).
    """
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print exactly what would be run/removed, without running or removing anything",
    )


def _add_uninstall_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for ``uninstall``. Without ``--yes`` every step shows
    what it changes and asks first."""
    _add_claude_root_arg(sub)
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="show every step and change, without changing anything",
    )
    sub.add_argument(
        "--yes",
        action="store_true",
        help="make the settings.json and service changes without asking (they are still printed)",
    )
    sub.add_argument(
        "--revert-changes",
        action="store_true",
        dest="revert_changes",
        help="also undo every change 'apply' made that is still in place, newest first",
    )
    sub.add_argument(
        "--delete-data",
        action="store_true",
        dest="delete_data",
        help="also delete this tool's data folder (database, snapshots, usage log, profiles and backups)",
    )


#: ``capture``'s actions; "status" is the default.
CAPTURE_ACTIONS = (
    "status", "on", "off", "level", "enable", "disable", "connect", "remove", "feedback", "brief", "prune", "refresh"
)

#: ``capture <action> on|off`` -> the skill it adds or removes, what the
#: change is called, and what it turns on.
_SKILL_SWITCHES = {
    "feedback": (capture_catalogue.FEEDBACK_SKILL, "Feedback", "the /tl-feedback skill and its status-line reminder"),
    "brief": (capture_catalogue.BRIEF_SKILL, "Brief templates", "the /tl-brief skill"),
}


def _add_capture_args(sub: argparse.ArgumentParser) -> None:
    """Arguments for ``capture``. Changes to this tool's own config.toml
    are made after the cost warning (and a yes when capture uses more
    tokens); settings.json changes always show the diff and ask."""
    _add_claude_root_arg(sub)
    sub.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=CAPTURE_ACTIONS,
        help="status (default); on; off; level LEVEL; enable/disable METRIC...; connect (add the hook entries "
        "the chosen metrics need to settings.json); remove (switch off and take the entries out); "
        "feedback on|off (the /tl-feedback skill and its status-line reminder); "
        "brief on|off (the /tl-brief skill, which checks a request against its checklist); "
        "prune (delete signal files, capture-log.jsonl records and usage-log.csv rows older than "
        f"retention_days, or {SIGNAL_RETENTION_DEFAULT_DAYS} days by default); "
        "refresh (work out the coaching notes' split points from your last 30 days now; the dashboard's "
        "service does it daily)",
    )
    sub.add_argument(
        "values",
        nargs="*",
        metavar="VALUE",
        help="the level for 'level'; metric ids for 'enable' and 'disable' (see 'claudeglass capture status'); "
        "on or off for 'feedback' and 'brief'",
    )
    sub.add_argument(
        "--level",
        choices=capture_catalogue.LEVELS[1:],
        default=None,
        help="for 'on': the level to use (default: essentials, or the current level when already on)",
    )
    # --until DATE (the shared option) sets the end time instead.
    sub.add_argument(
        "--for",
        dest="for_duration",
        metavar="DURATION",
        default=None,
        help="switch capture off by itself after this long: a number and h, d or w (e.g. 7d); "
        "--until DATE sets an ISO 8601 end date or time instead",
    )
    sub.add_argument(
        "--no-limit",
        action="store_true",
        dest="no_limit",
        help="turning capture on (with neither --for nor --until) runs it until you switch it off, instead of "
        f"the default {onboarding.DEFAULT_CAPTURE_TIMEBOX_DAYS}-day time-box (CAP-8: turning it on with none of "
        "these three switches itself off after that many days, so it can't run forever unnoticed)",
    )
    sub.add_argument(
        "--sample",
        type=int,
        choices=CAPTURE_SAMPLES,
        default=None,
        help="capture this share of sessions, in percent (each session is in or out for its whole length)",
    )
    sub.add_argument("--yes", action="store_true", help="make the changes without asking (they are still printed)")
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="show what would change, without changing anything",
    )


def _add_snapshot_config_args(sub: argparse.ArgumentParser) -> None:
    """Extra flags for the ``snapshot-config`` subcommand only (WP7). Every
    other subcommand stays a bare stub, so this is added just for this one
    subparser rather than the shared common parser.
    """
    group = sub.add_mutually_exclusive_group()
    group.add_argument(
        "--print-hook",
        action="store_true",
        help="print the SessionStart settings.json fragment (Windows and POSIX)",
    )
    group.add_argument(
        "--install-hook",
        action="store_true",
        help="copy hooks/snapshot-config.py into <config-dir>/hooks/ (<config-dir> "
        "defaults to ~/.claude/claudeglass)",
    )
    sub.add_argument(
        "--managed-path",
        metavar="PATH",
        default=None,
        help="override the platform managed-settings.json path (fix 7; "
        "default is the platform's own policy-file location)",
    )
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help="run the hook for this project directory instead of the current one "
        "(schema 2, plan 'Configuration layers' section). Named --project-dir, "
        "not --project, because the common --project flag already means "
        "'a repeatable project slug to filter by'.",
    )
    sub.add_argument(
        "--min-interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="skip the write when the newest snapshot for this project is both "
        "younger than this and has identical content (default: 300; the hook "
        "script itself already accepts this flag -- fix #13 exposes it here too)",
    )


def _add_apply_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``apply`` subcommand (v0.3 milestone, ``profiles/apply.py``):
    apply a profile's allowlisted settings/agent/env levers to a project
    or the current user, dry-run its diff first, or revert/list a
    previous apply. ``profile`` is a catalogue id (``profiles.catalogue``)
    or a path to a profile TOML file; it is optional only because
    ``--revert``/``--list-backups`` don't need one.

    Named ``--project-dir``, not ``--project``: the common ``--project``
    flag every subcommand already has means "a repeatable project slug
    to filter a report by" (same collision, same resolution, as
    ``snapshot-config``/``probe-config``'s own ``--project-dir``).
    """
    sub.add_argument(
        "profile", nargs="?", metavar="PROFILE", help="catalogue id or path to a profile TOML file"
    )
    sub.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=None,
        dest="set_values",
        help="change one allowlisted setting instead of applying a profile (repeatable); "
        "a list value is comma-separated, e.g. tools=Read,Grep",
    )
    sub.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="with --set: change this agent's frontmatter (.claude/agents/NAME.md) instead of settings.json",
    )
    sub.add_argument(
        "--scope",
        choices=("user", "project-local", "repo"),
        default=None,
        help="default: user, or project-local when --project-dir is given",
    )
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        dest="project_dir",
        help="project directory for a project-local/repo scope",
    )
    sub.add_argument(
        "--claude-root",
        metavar="PATH",
        default=None,
        dest="claude_root",
        help="the Claude Code directory holding settings.json/agents/ for user scope "
        "(default: $CLAUDE_CONFIG_DIR, else ~/.claude -- see cli._resolve_claude_root; "
        "deliberately independent of --config-dir, which is this tool's own directory "
        "and may be pointed anywhere)",
    )
    sub.add_argument(
        "--dry-run", action="store_true", help="print the diff and how to apply it, without writing anything"
    )
    sub.add_argument(
        "--launch",
        action="store_true",
        help="write a one-session settings-only overlay instead of a persisted apply",
    )
    sub.add_argument(
        "--allow-tracked",
        action="store_true",
        dest="allow_tracked",
        help="allow writing a target file that is already tracked by git",
    )
    sub.add_argument(
        "--revert", metavar="TS", default=None, help="undo a previous apply, named by its backup timestamp"
    )
    sub.add_argument(
        "--ignore-changes",
        action="store_true",
        dest="ignore_changes",
        help="with --revert: restore the backup even if the file was edited after the apply",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="create a missing agent frontmatter file from scratch instead of refusing",
    )
    sub.add_argument(
        "--list-backups",
        action="store_true",
        dest="list_backups",
        help="list previous applies (timestamp, profile, scope) and exit",
    )


def _add_claude_root_arg(sub: argparse.ArgumentParser) -> None:
    """``--claude-root`` for the commands that read or change Claude
    Code's own ``settings.json`` (``init``, ``uninstall``, ``changes``);
    ``apply`` declares its own with the same meaning."""
    sub.add_argument(
        "--claude-root",
        metavar="PATH",
        default=None,
        dest="claude_root",
        help="the Claude Code folder holding settings.json (default: $CLAUDE_CONFIG_DIR, else ~/.claude; "
        "never worked out from --config-dir)",
    )


def _add_init_args(sub: argparse.ArgumentParser) -> None:
    _add_claude_root_arg(sub)
    sub.add_argument(
        "--answers",
        metavar="FILE",
        default=None,
        help="a JSON file answering some or all of init's questions (any key it "
        "omits falls back to interactive prompting, or a derived default under "
        "--non-interactive)",
    )
    sub.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt on stdin, and make the changes without asking for a yes; any question --answers "
        "doesn't cover uses a derived default, printed as '(derived) ...' so nothing is guessed silently",
    )
    sub.add_argument(
        "--advanced",
        action="store_true",
        help="also ask about projects to leave out, launch overlays, a shared .claude folder, the time zone, "
        "where applied changes go, the capture window and WSL folders, and the full metrics capture and "
        "feedback questions (the default asks only how you pay, connecting, the logon task and sharper tips)",
    )
    sub.add_argument(
        "--no-install",
        action="store_true",
        help="skip connecting to Claude Code: settings.json and Claude Code's skills folder are left alone",
    )
    sub.add_argument(
        "--repair-hook",
        action="store_true",
        dest="repair_hook",
        help="fix a SessionStart hook command that cannot run (a path broken by JSON escaping, "
        "a missing interpreter or a %%VARIABLE%%), without asking (settings.json is backed up first)",
    )
    sub.add_argument(
        "--connect",
        action="store_true",
        help="connect to Claude Code without asking: add the config snapshot hook (and a statusline, if you "
        "have none) to its settings.json; the change is still shown and the file backed up first. Needed for "
        "--non-interactive to change settings.json at all",
    )
    service_group = sub.add_mutually_exclusive_group()
    service_group.add_argument(
        "--install-service",
        action="store_true",
        dest="install_service",
        help="register 'serve' to start at logon/boot without asking (also the "
        "--non-interactive default, which is otherwise 'no')",
    )
    service_group.add_argument(
        "--no-service",
        action="store_true",
        dest="no_service",
        help="skip the 'start the dashboard at logon?' step entirely -- no "
        "question asked, nothing installed",
    )
    sub.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="show every change init would make (the settings.json diff and the logon task included) and "
        "write nothing: no config.toml, no hook files, no baseline",
    )
    sub.add_argument(
        "--capture-level",
        choices=capture_catalogue.LEVELS,
        default=None,
        help="answer the metrics capture question without asking: off, or the level to turn on "
        "(capture uses tokens; 'claudeglass capture status' shows how many)",
    )
    sub.add_argument(
        "--feedback",
        choices=("on", "off"),
        default=None,
        help="answer the feedback question without asking: add the /tl-feedback skill and its status-line "
        "reminder (on), or not (off)",
    )
    sub.add_argument(
        "--capture-no-limit",
        action="store_true",
        dest="capture_no_limit",
        help="answer the metrics capture time-box question without asking: no time limit, so capture runs "
        f"until you switch it off (default: it switches itself off after "
        f"{onboarding.DEFAULT_CAPTURE_TIMEBOX_DAYS} days; --capture-for picks another length up front, or "
        "'claudeglass capture on --for 30d' changes it once it's on)",
    )
    sub.add_argument(
        "--capture-for",
        dest="capture_for",
        metavar="DURATION",
        default=None,
        help="answer the metrics capture time-box question without asking: switch capture off by itself after "
        "this long, a number and h, d or w (e.g. 30d), instead of the "
        f"{onboarding.DEFAULT_CAPTURE_TIMEBOX_DAYS}-day default; not with --capture-no-limit",
    )


def _add_baseline_args(sub: argparse.ArgumentParser) -> None:
    # Note: --days is already provided by the common parent parser
    # (mutually exclusive with --since) -- every subcommand inherits it,
    # baseline included, so it is not redefined here.
    sub.add_argument(
        "--finalise",
        action="store_true",
        help="accept this baseline even if the onboarding capture window hasn't finished",
    )
    sub.add_argument("--list", action="store_true", dest="list_baselines", help="list every saved baseline")
    sub.add_argument("--show", metavar="ID", default=None, help="print one saved baseline's onboarding report")


def _add_probe_config_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``probe-config`` subcommand (schema 2): the same scan
    ``snapshot-config`` does, for an arbitrary project directory, without a
    session and without writing anything.
    """
    sub.add_argument(
        "--project-dir",
        metavar="PATH",
        default=None,
        help="project directory to scan (default: the current directory). Named "
        "--project-dir, not --project, because the common --project flag "
        "already means 'a repeatable project slug to filter by'.",
    )
    sub.add_argument(
        "--managed-path",
        metavar="PATH",
        default=None,
        help="override the platform managed-settings.json path (default is the "
        "platform's own policy-file location)",
    )


def _add_import_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``import`` subcommand (v0.3 Task 1): one or more
    already-built team-aggregate documents (``export --aggregate``'s own
    output) to validate and copy into ``<config_dir>/team/``.
    """
    sub.add_argument(
        "files", nargs="+", metavar="FILE", help="team-aggregate JSON document(s) to import"
    )


def _add_team_report_args(sub: argparse.ArgumentParser) -> None:
    """Flags for the ``team-report`` subcommand (v0.3 Task 1): reads
    only already-imported documents under ``<config_dir>/team/`` (see
    ``import``), so it needs no project/window selection of its own --
    just the shared output flags. Deviation (report, don't silently
    resolve): the plan's ``[--md|--json|--html]`` gets no explicit
    ``--md`` switch -- "no output flag" already means Markdown for
    every other report-like/compare-like subcommand in this CLI
    (``_add_compare_output_args``), so adding one here would be the one
    inconsistent flag in the whole surface.
    """
    _add_compare_output_args(sub)
    sub.add_argument(
        "--min-sessions",
        type=int,
        default=5,
        metavar="N",
        help="minimum sessions required in a machine's own group row before a "
        "team-report cell shows a number rather than 'n<N' (default: 5)",
    )


#: Options whose value is a project slug, or a pattern over slugs.
_SLUG_OPTIONS = frozenset({"--project", "--project-family"})


class _Parser(argparse.ArgumentParser):
    """Takes a project slug that starts with ``-`` as the value it is.
    Every slug on Linux and macOS does (``/home/alice/shop`` is
    ``-home-alice-shop``), and argparse would read it as an option and
    stop with "expected one argument"."""

    def parse_known_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        joined: list[str] = []
        i = 0
        while i < len(args):
            value = args[i + 1] if i + 1 < len(args) else ""
            # A single dash and more than one letter: not -h, not --all-projects.
            if args[i] in _SLUG_OPTIONS and len(value) > 2 and value[0] == "-" and value[1] != "-":
                joined.append(f"{args[i]}={value}")
                i += 2
            else:
                joined.append(args[i])
                i += 1
        return super().parse_known_args(joined, namespace)


def _make_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    parser = _Parser(prog="claudeglass")
    parser.add_argument(
        "--version", action="version", version=f"claudeglass {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for name in SUBCOMMANDS:
        help_text = {
            "snapshot-config": "capture Claude Code config (see config-diff to compare)",
            "report": "full report (default)",
            "sessions": "sessions-only report view",
            "recache": "RE-CACHE-only report view",
            "ttl": "TTL break-even-only report view",
            "limits": "usage-limits-only report view",
            "carry": "context-carry-cost-only report view (cost of carrying tool results across later turns)",
            "compaction-sim": "autoCompactWindow-sweep-only report view (modelled cost under other window settings)",
            "model-swap": "model-swap-only report view (modelled saving from a cheaper model tier)",
            "waste": "wasted-turn-spend-only report view (turns whose output was never used)",
            "quality": "quality-signals-only report view (failed agent runs, failed tool calls, corrections, "
            "compared by model and effort)",
            "compactions": "compactions-only report view",
            "config-diff": "compare sessions grouped by a config key's value",
            "pricing-check": "print the resolved rate card's provenance and rate table",
            "probe-config": "scan a project's config layers without a session (schema 2)",
            "log-usage": "append a pasted get_usage JSON payload to the usage log",
            "probe": "content-free schema histogram of a project or file",
            "statusline": "Claude Code statusLine handler (reads stdin JSON)",
            "export": "export digests as csv-flat, json or otel-jsonl (aggregate-only by default)",
            "monthly-report": "write a monthly Markdown/HTML finance report",
            "compare": "A/B compare two arms of sessions (window/key/profile/project), stratified by purpose+mode",
            "reconcile": "compare local usage/cost accounting against an Admin API CSV export, offline",
            "scrub-fixture": "scrub a real session into a privacy-safe test fixture",
            "apply": "apply a profile's settings/agent/env levers to a project or your user config",
            "init": "detect + ask (or derive) config, write config.toml, run an initial baseline",
            "baseline": "capture/list/show an onboarding baseline (mode mix, suggested profile, projected saving)",
            "serve": "run the local JSON API + watcher service",
            "install-service": "register 'serve' to start at logon/boot (Scheduled Task / systemd user unit / LaunchAgent)",
            "uninstall-service": "remove a logon/boot registration made by install-service (or by init)",
            "update": "install the newest version, restart the dashboard on it and tidy up what an older one left",
            "changes": "list what this tool has installed and changed, and the command that undoes each",
            "status": "check the setup works: how you pay, the Claude Code connection, the dashboard, sharper tips",
            "review": "review your CLAUDE.md files or skills: size, how often each is sent, cost, and fixes",
            "check": "quick actions: answer one token question (or all of them) with evidence and fixes",
            "uninstall": "remove the hook, statusline and logon service, optionally undo applied changes and delete data",
            "import": "validate and copy team-aggregate document(s) into <config_dir>/team/",
            "team-report": "cross-machine comparison built from every imported team document",
            "capture": "metrics capture: have Claude tag its replies so suggestions fit how you work (uses tokens)",
            "backtest": "read-only: predictions logged by the dashboard's whatif, matched to what actually "
            "happened and judged (EST-P4) -- see the dashboard for the judging itself",
        }.get(name, f"{name} (not implemented yet)")
        sub = subparsers.add_parser(name, parents=[common], help=help_text)
        if name == "pricing-check":
            sub.add_argument(
                "--models",
                metavar="ID,ID,...",
                help="comma-separated model ids to resolve and report",
            )
        if name == "snapshot-config":
            _add_snapshot_config_args(sub)
        if name == "probe-config":
            _add_probe_config_args(sub)
        if name == "apply":
            _add_apply_args(sub)
        if name == "review":
            sub.add_argument(
                "what",
                choices=("claude-md", "skills"),
                help="claude-md: every CLAUDE.md file and rule; skills: every skill Claude Code lists",
            )
        if name == "check":
            from .quick_actions import CHECK_IDS

            sub.add_argument(
                "id",
                nargs="?",
                choices=CHECK_IDS,
                help="the check to run in full; leave it out for every check's one-line answer",
            )
        if name in _REPORT_LIKE_COMMANDS:
            _add_report_output_args(sub, allow_patch_set=(name == "report"))
        if name == "config-diff":
            _add_config_diff_args(sub)
        if name == "scrub-fixture":
            _add_scrub_fixture_args(sub)
        if name == "probe":
            _add_probe_args(sub)
        if name == "statusline":
            _add_statusline_args(sub)
        if name == "export":
            _add_export_args(sub)
        if name == "monthly-report":
            _add_monthly_report_args(sub)
        if name == "compare":
            _add_compare_args(sub)
        if name == "reconcile":
            _add_reconcile_args(sub)
        if name == "serve":
            _add_serve_args(sub)
        if name == "install-service":
            _add_install_service_args(sub)
        if name == "uninstall-service":
            _add_uninstall_service_args(sub)
        if name == "update":
            _add_update_args(sub)
        if name == "uninstall":
            _add_uninstall_args(sub)
        if name in ("changes", "status"):
            _add_claude_root_arg(sub)
        if name == "init":
            _add_init_args(sub)
        if name == "baseline":
            _add_baseline_args(sub)
        if name == "import":
            _add_import_args(sub)
        if name == "team-report":
            _add_team_report_args(sub)
        if name == "capture":
            _add_capture_args(sub)
    return parser


# -- shared config-dir / project / corpus plumbing --------------------------


def _resolve_config_dir(cli_arg: str | Path | None) -> Path:
    """``--config-dir`` wins -- and IS the claudeglass directory itself
    everywhere (fix config-dir: one meaning across the hook, the CLI and
    ``snapshots.py`` -- see ``snapshots.load_snapshots``'s docstring)
    -- else ``$CLAUDE_CONFIG_DIR/claudeglass``; else ``~/.claude/claudeglass``.
    Mirrors ``config.py``'s own ``_default_config_dir``/
    ``_resolve_config_dir`` -- each module in this package keeps its own
    copy of this small lookup rather than sharing one (see e.g.
    ``tools/log_usage.py``'s module docstring), and the CLI is no
    exception.
    """
    if cli_arg:
        return Path(cli_arg)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / "claudeglass"


def _resolve_claude_root(cli_arg: str | Path | None) -> Path:
    """The Claude Code root directory: the one that directly holds
    ``settings.json`` and ``agents/``. ``--claude-root`` wins; else
    ``$CLAUDE_CONFIG_DIR``; else ``~/.claude``.

    Delegates to :func:`discovery.claude_root`, the one rule every
    command uses to find ``settings.json`` (``init``, ``uninstall``,
    ``changes``, the dashboard's hook checks, the snapshot hook).

    Fix B3: deliberately NOT derived from ``_resolve_config_dir``'s
    result. ``apply`` used to compute ``home = config_dir.parent``,
    which happens to equal this exact directory only when ``config_dir``
    took its own untouched default (``<claude-root>/claudeglass``) --
    ``--config-dir``/``config.toml`` can point this tool's own
    claudeglass directory anywhere, at which point ``.parent`` is just
    some unrelated directory. With the (also then-wrong) default,
    ``apply``'s ``home`` ended up equal to the Claude root itself, and
    ``profiles.apply._resolve_settings_path`` appended another
    ``.claude/`` on top of it -- so a user-scope apply silently wrote
    ``<claude-root>/.claude/settings.json`` (``~/.claude/.claude/settings.json``
    in the default layout) while printing "Applied ..." and leaving the
    real ``~/.claude/settings.json`` untouched.
    """
    return discovery.claude_root(cli_arg)


def _config_dir_args(config_dir: Path) -> str:
    """`` --config-dir "<absolute path>"`` for a hook or statusline
    command when this tool's data folder is not the default
    ``<claude-root>/claudeglass``, else an empty string. Without it the
    hook and statusline, which Claude Code starts on its own, would
    write snapshots and the usage log to the default folder while the
    CLI and dashboard read ``--config-dir``."""
    config_dir = Path(config_dir).resolve()
    if config_dir == (discovery.claude_root() / "claudeglass").resolve():
        return ""
    return f' --config-dir "{config_dir}"'


def _priced_turns(result: TranscriptResult):
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns, but keeps an estimated compaction call, which is
    spend -- see ``parse.py``). Deliberately duplicated rather than imported
    from ``report.py`` -- the same one-line-helper convention
    ``workflows.py``/``phases.py``/``report.py`` itself document.
    """
    return [t for t in result.turns if t.turn_index > 0]


def _service_projects_roots(args: argparse.Namespace) -> list[Path]:
    """The ``--projects-root`` folders the logon service is registered
    with: the ones given, or this computer's default. ``serve`` adds
    ``config.toml``'s ``extra_projects_roots`` itself each time it
    starts, so they are never baked into the registration."""
    return [Path(p) for p in args.projects_root] if args.projects_root else [discovery.projects_root()]


def _resolve_project_dirs_for_args(args: argparse.Namespace, config: Config) -> tuple[str, list[Path]]:
    roots = discovery.projects_roots(args.projects_root, config.extra_projects_roots)
    root = ", ".join(str(r) for r in roots)
    slugs = list(args.project) if args.project else None
    if not args.all_projects and not args.project_family and not slugs:
        slugs = [discovery.slug_for(os.getcwd())]
    project_dirs = discovery.resolve_project_dirs(
        roots,
        slugs=slugs,
        all_projects=args.all_projects,
        family_regex=args.project_family,
        exclude_projects=config.exclude_projects,
    )
    return root, project_dirs


def _window_description(args: argparse.Namespace) -> str:
    if args.since or args.until:
        start = f"since {args.since}" if args.since else "since the beginning"
        end = f"until {args.until}" if args.until else "until now"
        return f"{start} {end}"
    if args.days:
        return f"last {args.days} days"
    return "all time"


def _load_config_and_pricing(args: argparse.Namespace) -> tuple[Config | None, Pricing | None, Path, int | None]:
    """Resolve the config dir, load ``config.toml`` and the pricing rate
    card. Returns ``(config, pricing, config_dir, exit_code)`` -- on
    failure the first two are ``None`` and ``exit_code`` (2, always a bad
    input) is set, with the reason already printed to stderr.
    """
    config_dir = _resolve_config_dir(args.config_dir)
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"claudeglass: {exc}", file=sys.stderr)
        return None, None, config_dir, 2

    # Fix R24: --tz overrides config.toml's tz for this run only, the
    # same "explicit flag wins over the file" convention --pricing/
    # --config-dir already follow. classify.classify_session/usage.py's
    # _to_local both already fall back to the machine's local zone when
    # a zone name can't be resolved -- deliberately, since a bare
    # Windows install with no tzdata package can't resolve *any* named
    # zone (see classify.py's module docstring) and that's a machine
    # limitation, not a bad value. So this only rejects a --tz value
    # outright when the machine actually has a populated tz database to
    # check it against and the name genuinely isn't in it (a real
    # command-line typo); otherwise it's passed through uncontested and
    # degrades the same way a config.toml value already does.
    tz_override = getattr(args, "tz", None)
    if tz_override is not None:
        known_zones = available_timezones()
        if known_zones and tz_override not in known_zones:
            print(f"claudeglass: --tz {tz_override!r} is not a known IANA zone", file=sys.stderr)
            return None, None, config_dir, 2
        config.tz = tz_override

    pricing_path = args.pricing or config.pricing_path
    try:
        rates = load_pricing(path=pricing_path, config_dir=config_dir)
    except PricingError as exc:
        print(f"claudeglass: {exc}", file=sys.stderr)
        return None, None, config_dir, 2

    return config, rates, config_dir, None


def _print_corpus_stats(corpus: Corpus) -> None:
    print(
        f"[corpus] files={corpus.total_files} bytes={corpus.total_bytes} "
        f"cache_hits={corpus.cache_hits} cache_misses={corpus.cache_misses} "
        f"elapsed_s={corpus.elapsed_s:.3f}",
        file=sys.stderr,
    )


def _merge_dashboard_marks(config_dir: Path, overrides: dict) -> tuple[dict, dict]:
    """``(overrides, ratings)``: ``overrides`` with the dashboard's
    session tags merged over it, and the dashboard's session ratings,
    both read from ``<config-dir>/service.db`` without writing to it."""
    from .service.serve import STORE_FILENAME
    from .service.store import read_session_marks

    tags, ratings = read_session_marks(config_dir / STORE_FILENAME)
    if not tags:
        return overrides, ratings
    merged = {sid: dict(entry) for sid, entry in overrides.items()}
    for session_id, entry in tags.items():
        merged.setdefault(session_id, {}).update(entry)
    return merged, ratings


def _load_corpus_for_args(
    args: argparse.Namespace, config: Config, config_dir: Path, project_dirs: list[Path]
) -> Corpus:
    # Fix #8: wire the A3 read-target-hash salt up to the actual corpus
    # load -- previously nothing in src/ ever called set_salt/
    # load_or_create_salt, so Turn.read_target_hashes was always empty in
    # every shipped code path. load_corpus threads this through to both
    # the in-process (jobs == 1) parse calls and, for jobs > 1, every
    # ProcessPoolExecutor worker's own initializer. Loaded before the
    # cache below (SEC-P8) so a cache entry's own salt_fp header field
    # can be checked/stamped against the same salt this load will use.
    salt = load_or_create_salt(config_dir)
    cache = None
    if not args.no_cache:
        cache = DigestCache(config_dir, salt=salt)
        if args.rebuild_cache:
            cache.purge(all=True)
        else:
            # ROB-P4/P5: a stale PARSER_VERSION folder is never read
            # again once this version's own folder exists, so sweeping
            # it here (once per real corpus load, cheap -- a handful of
            # directory stats) keeps <config-dir>/cache/ from growing a
            # new dead folder forever every time PARSER_VERSION bumps.
            # Skipped on --rebuild-cache: that already wiped this
            # version's own folder, and a prune right after would just
            # be extra directory churn for no benefit.
            cache.prune_stale_versions()
    corpus = load_corpus(
        project_dirs,
        days=args.days,
        since=args.since,
        until=args.until,
        limit=args.limit,
        window_by=args.window_by,
        cache=cache,
        jobs=args.jobs,
        exclude_projects=config.exclude_projects,
        salt=salt,
    )
    # Fix R21: --quiet was accepted by argparse (mutually exclusive with
    # --verbose) but never actually consulted anywhere -- a silent no-op
    # flag. The CLI's argparse wiring already keeps a human from passing
    # both at once, but this function's own contract shouldn't depend on
    # that: guard explicitly so --quiet reliably suppresses this stderr
    # diagnostic even if a future caller builds/mutates the Namespace
    # itself (e.g. a script driving this function directly) rather than
    # going through argparse's mutual-exclusion check.
    if args.verbose and not args.quiet:
        _print_corpus_stats(corpus)
    return corpus


def _print_table(table, currency: str) -> None:
    """Print one :class:`~claudeglass.model.Table` as a plain-text,
    fixed-width table (the same rendering ``pricing-check`` has always
    used). Shared by ``pricing-check`` and ``config-diff``.
    """
    headers = [column.label for column in table.columns]
    formatted_rows = [
        [
            format_cell(value, column.kind, currency=currency)
            for value, column in zip(row, table.columns)
        ]
        for row in table.rows
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in formatted_rows)) if formatted_rows else len(headers[i])
        for i in range(len(headers))
    ]
    print(table.title)
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in formatted_rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    for note in table.notes:
        print(f"note: {pages.plain(note)}")


def _resolve_generated_at(args: argparse.Namespace) -> str | None:
    """``args.generated_at`` (``--generated-at``) if given; else
    ``SOURCE_DATE_EPOCH`` (the same reproducible-build env var convention
    other tooling already looks for), interpreted as an integer Unix
    timestamp; else ``None`` (the callee's own "current instant"
    default). Shared by ``export`` (nit 19) and ``monthly-report``
    (finding 11) -- both subcommands' output is otherwise only
    "identical apart from one wall-clock line" across repeated runs.
    """
    generated_at = args.generated_at
    if generated_at is None:
        source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if source_date_epoch:
            try:
                generated_at = (
                    datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (ValueError, OverflowError, OSError):
                generated_at = None
    return generated_at


# -- report-like subcommands (report/sessions/recache/ttl/compactions) -----


def _render_patch_set_text(model, args: argparse.Namespace) -> str | None:
    """The recommendation patch-set text for ``--patch-set``, or ``None``
    when the flag wasn't passed or ``recommend`` (an optional dependency,
    see the module docstring) isn't importable. Computed once and reused
    across whichever output modes are active, since where it lands
    differs per mode -- see ``_emit_report_outputs``.
    """
    if not getattr(args, "patch_set", False):
        return None
    if importlib.util.find_spec("claudeglass.recommend") is None:
        return None
    from . import recommend  # local import: optional dependency, see module docstring

    return recommend.render_patch_set(model.recommendations)


def _emit_report_outputs(model, args: argparse.Namespace) -> None:
    # Fix cli/patch-set-json: with --json, the patch set used to be
    # printed as trailing text *after* the JSON blob, which made
    # `--json --patch-set` together produce output no `json.loads`
    # could parse. Each output mode now gets the patch set through its
    # own channel: embedded as a JSON string key for --json, a sibling
    # `patch-set.txt` file for --html/--csv-dir (a file has no "after
    # the blob" to corrupt), and unchanged trailing stdout text for the
    # default Markdown mode.
    patch_text = _render_patch_set_text(model, args)

    if getattr(args, "json", False):
        # main leaves JSON output alone (a path in the prefix would break
        # it as text); its commands are swapped string by string here.
        print(invocation.rewrite_rendered(render_json(model, patch_set=patch_text), "json"))
    else:
        text = render_markdown(model, explain=getattr(args, "explain", False))
        print(text, end="")
        if patch_text:
            print()
            print(patch_text)

    html_path = getattr(args, "html", None)
    if html_path:
        Path(html_path).write_text(invocation.rewrite_rendered(render_html(model), "html"), encoding="utf-8")
        if patch_text:
            Path(html_path).parent.joinpath("patch-set.txt").write_text(patch_text, encoding="utf-8")

    csv_dir = getattr(args, "csv_dir", None)
    if csv_dir:
        write_csv_dir(model, csv_dir)
        if patch_text:
            Path(csv_dir).joinpath("patch-set.txt").write_text(patch_text, encoding="utf-8")


def _cmd_report_like(args: argparse.Namespace, include: set[str] | None, *, emit=None) -> int:
    command = args.command or DEFAULT_SUBCOMMAND

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claudeglass {command}: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claudeglass {command}: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    snaps = snapshots.load_snapshots(config_dir) or None
    projects = tuple(p.name for p in project_dirs)

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return 2
    # The dashboard's Sessions-tab tags and ratings, read without writing,
    # so the CLI report classifies and rates sessions as the dashboard
    # does (a tag set there wins over sessions.toml, as in api.py).
    session_overrides, ratings = _merge_dashboard_marks(config_dir, session_overrides)

    # S1-exports: when the statusline has been logging ground truth
    # (context_window/cache) into <config_dir>/usage-log.csv, feed those
    # rows into build_report -- statusline.load_usage_log_ground_truth is
    # the tolerant reader that copes with both an old-format file (no
    # cache_* columns yet) and a new one, per its own docstring.
    #
    # Fix for review finding 8: this used to hand build_report every row
    # ever logged, from every project, ignoring this invocation's own
    # --days/--since/--until window and --project selection -- the only
    # table in the report that didn't respect either. Scoped now to
    # exactly the sessions this invocation actually loaded (project
    # selection falls out of that for free: project_dirs already
    # reflects --project/--all-projects/--project-family) and to the
    # resolved --days/--since/--until window via each row's own
    # logged_at (discovery._resolve_window is the same resolution
    # discovery.find_sessions itself uses -- see service/rebuild.py for
    # existing precedent importing this private helper cross-module).
    from .discovery import _resolve_window

    since_dt, until_dt = _resolve_window(args.days, args.since, args.until)
    usage_log_rows = statusline_mod.scoped_usage_log_rows(
        config_dir / "usage-log.csv", {b.session_id for b in corpus.sessions}, since_dt, until_dt
    )

    # v0.3 Task 2: --baseline <id|latest> resolves a saved baseline.py
    # record for build_report's own baseline_comparison section. This is
    # a plain-dict/no-Path lookup (baseline_mod.list_baselines/
    # load_baseline), so it's resolved here rather than inside
    # build_report itself -- this is a separate flag-specific lookup from
    # the v4-wiring-round ``config_dir`` build_report now does accept
    # (see report.py's own module docstring's deviation note): that one
    # is narrowly for waste.WasteStats's own salted session-id hash, not
    # a general "build_report may now load files itself" opening, so
    # --baseline's own file lookup still happens here rather than moving
    # inside build_report. When resolution fails, the section is simply
    # omitted and a note is threaded through as baseline_note instead of
    # erroring -- matches the plan's "when no baseline exists, the
    # section is omitted" wording.
    baseline_record = None
    baseline_note = None
    baseline_arg = getattr(args, "baseline", None)
    if baseline_arg:
        if baseline_arg == "latest":
            saved = baseline_mod.list_baselines(config_dir)
            baseline_record = saved[-1] if saved else None
            if baseline_record is None:
                baseline_note = (
                    "--baseline latest requested but no baseline has been saved yet -- run "
                    "`claudeglass baseline` first."
                )
        else:
            baseline_record = baseline_mod.load_baseline(config_dir, baseline_arg)
            if baseline_record is None:
                baseline_note = (
                    f"--baseline {baseline_arg!r} requested but no such baseline was found -- run "
                    "`claudeglass baseline --list` to see what's saved."
                )

    try:
        model = build_report(
            corpus,
            rates,
            config,
            projects=projects,
            window=window,
            group_by=args.group_by,
            phases=getattr(args, "phases", False),
            snapshots=snaps,
            include=include,
            session_overrides=session_overrides,
            usage_log_rows=usage_log_rows,
            baseline_record=baseline_record,
            baseline_note=baseline_note,
            config_dir=config_dir,
            ratings=ratings,
        )
    except ScorecardError as exc:
        # Fix R20: a misordered [thresholds.scorecard] override in
        # config.toml used to surface as a raw traceback out of
        # scorecard.build_section (called deep inside build_report);
        # give it the same clean one-line-and-exit-2 treatment as every
        # other user-facing config error in this function.
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return 2
    if emit is not None:
        return emit(model, config_dir, window)
    _emit_report_outputs(model, args)
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """``review claude-md|skills``: the dashboard's Context files review
    as Markdown. File text and skill descriptions are read now, never
    stored."""
    from . import claude_md_review, skills_review
    from .units import Units

    def emit(model, config_dir, window) -> int:
        units = model.units or Units()
        period = _period_phrase(window)
        context = model.context_files or {}
        if args.what == "skills":
            print(skills_review.render_markdown(skills_review.review(config_dir, context, units, period)))
        else:
            review = claude_md_review.build_review(config_dir, context)
            print(claude_md_review.render_markdown(review, units, period))
        return 0

    return _cmd_report_like(args, {"overview"}, emit=emit)


def _period_phrase(window: str) -> str:
    """"last 30 days" -> "over the last 30 days", for amounts."""
    if window.startswith("since"):
        return window
    return f"over the {window}" if window.startswith("last") else f"over {window}"


def _cmd_check(args: argparse.Namespace) -> int:
    """``check [ID]``: the dashboard's Quick actions as Markdown -- every
    check's one-line answer, or one check in full with its evidence,
    fixes and tips. Nothing is changed; fixes are prompts and dry-run
    commands."""
    from . import quick_actions
    from .units import Units

    def emit(model, config_dir, window) -> int:
        snapshot = snapshots.with_every_project_agents(snapshots.load_snapshots(config_dir))
        agents = snapshot.data.get("effective_agents") if snapshot is not None else None
        ctx = quick_actions.Context(
            model=model,
            units=model.units or Units(),
            period=_period_phrase(window),
            config_dir=Path(config_dir),
            effective=snapshots.effective_config_in_force(snapshot) if snapshot is not None else {},
            effective_agents=agents if isinstance(agents, dict) else {},
        )
        if args.id:
            print(quick_actions.render_markdown(quick_actions.run(args.id, ctx)))
            return 0
        marks = {"act": "Act", "ok": "OK", "no_data": "No data"}
        print(f"# Quick actions ({ctx.period})\n")
        for row in quick_actions.run_all(ctx):
            print(f"- **{marks[row['status']]}** `{row['id']}`: {row['question']} {pages.plain(row['summary'])}")
        print("\nRun `claudeglass check <id>` for the evidence and fixes.")
        return 0

    return _cmd_report_like(args, None, emit=emit)


# -- config-diff -------------------------------------------------------------


def _build_session_metrics(
    corpus: Corpus, rates: Pricing, recache_th, config: Config, session_overrides: dict
) -> list[dict]:
    """Per-session ``{session_id, first_ts, turns, cost, recache_cc,
    cc_total, compactions, span_s}`` dicts for
    :func:`~claudeglass.snapshots.build_config_diff_table`.

    ``report.py``'s own "config" section (used when a report is rendered
    with snapshots attached) builds the same shape internally, but only
    exposes it bundled into every changed key up to a fixed cap -- not
    as a reusable function, and not for a caller that wants exactly one
    named key regardless of whether it happens to have changed. This is
    a standalone recomputation from public APIs only (``classify``,
    ``recache.detect``, ``pricing.price_turn``), not a report.py
    private-function reuse.
    """
    metrics: list[dict] = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        # Fix R18: this used to hardcode {} here, so a manual
        # sessions.toml mode/purpose override -- honoured by every
        # other subcommand via _cmd_report_like's own
        # load_session_overrides(config_dir) -- was silently ignored
        # for config-diff alone.
        classification = classify.classify_session(bundle.top, bundle.subs, session_overrides, config.tz)
        record = classify.build_session_record(
            bundle.top, bundle.subs, bundle.workflows, classification, bundle.slug
        )

        cost = 0.0
        cc_total = 0
        turns = 0
        recache_cc = 0
        for tr in [bundle.top, *bundle.subs]:
            flagged = recache.detect(tr.turns, recache_th)
            recache_cc += sum(t.cache_creation_tokens for t in flagged)
            for turn in _priced_turns(tr):
                resolved = rates.resolve_model(turn.model)
                cost += price_turn(turn, resolved).total
                cc_total += turn.cache_creation_tokens
                turns += 1

        compactions = sum(1 for e in bundle.top.events if e.kind == EventKind.COMPACT_BOUNDARY)

        metrics.append(
            {
                "session_id": record.session_id,
                "first_ts": record.first_ts,
                "project_key": snapshots.snapshot_project_key(bundle.slug),
                "turns": turns,
                "cost": cost,
                "recache_cc": recache_cc,
                "cc_total": cc_total,
                "compactions": compactions,
                "span_s": record.span_s,
            }
        )
    return metrics


def _cmd_config_diff(args: argparse.Namespace) -> int:
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claudeglass config-diff: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claudeglass config-diff: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    snaps = snapshots.load_snapshots(config_dir)
    if not snaps:
        print(
            "claudeglass config-diff: no config snapshots found under "
            f"{config_dir / 'snapshots'}",
            file=sys.stderr,
        )
        return 1

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claudeglass config-diff: {exc}", file=sys.stderr)
        return 2
    session_overrides, _ratings = _merge_dashboard_marks(config_dir, session_overrides)

    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    session_metrics = _build_session_metrics(corpus, rates, recache_th, config, session_overrides)

    if args.auto_keys:
        changed_keys = sorted(snapshots.diff_keys(snaps).keys())
        if not changed_keys:
            print("No config keys changed across the available snapshots.")
            return 0
        tables = [
            snapshots.build_config_diff_table(session_metrics, snaps, key) for key in changed_keys
        ]
    else:
        tables = [snapshots.build_config_diff_table(session_metrics, snaps, args.key)]

    helptext.annotate_section(Section(key="config", title="", tables=tables), "subscription" if config.billing == "subscription" else "api")
    for table in tables:
        _print_table(table, rates.currency)
        print()
    return 0


# -- compare / reconcile (V3-compare) ----------------------------------------


def _wrap_section_as_report(
    section, rates: Pricing, config: Config, projects: tuple[str, ...], window: str, coverage_pct: float
) -> ReportModel:
    """Wrap one already-built :class:`~claudeglass.model.Section`
    (``compare``'s or ``reconcile``'s) in a minimal
    :class:`~claudeglass.model.ReportModel` so it can go out
    through the same Markdown/JSON/CSV/HTML renderers ``report``/
    ``sessions``/``recache``/etc. use via :func:`_emit_report_outputs`,
    rather than each having its own bespoke renderer. ``report.py``'s own
    ``build_report`` is not reusable here (it always assembles the whole
    fixed section list from a live corpus scan; a single already-built
    ``Section`` has nowhere to plug in), so this mirrors its ``meta``
    construction by hand instead.
    """
    meta = ReportMeta(
        tool_version=__version__,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=window,
        projects=tuple(sorted({discovery.redact_slug(p) for p in projects})),
        pricing=PricingMeta(
            path=rates.path,
            version=rates.version,
            sha8=rates.sha8,
            currency=rates.currency,
            coverage_pct=coverage_pct,
        ),
        thresholds={},
        billing_mode=config.billing,
        assumptions=[],
    )
    return ReportModel(meta=meta, sections=[section], recommendations=[], diagnostics=Diagnostics())


def _cmd_compare(args: argparse.Namespace) -> int:
    from . import compare as compare_mod

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    try:
        arm_a = compare_mod.parse_arm_spec(args.arm_a)
        arm_b = compare_mod.parse_arm_spec(args.arm_b)
    except ValueError as exc:
        print(f"claudeglass compare: {exc}", file=sys.stderr)
        return 2

    stratify_by = (
        None if args.stratify is None else tuple(s.strip() for s in args.stratify.split(",") if s.strip())
    )
    bad_keys = [k for k in stratify_by or () if k not in compare_mod.STRATIFY_CHOICES]
    if bad_keys:
        print(
            f"claudeglass compare: bad --stratify key(s) {bad_keys}: expected any of "
            f"{', '.join(compare_mod.STRATIFY_CHOICES)}",
            file=sys.stderr,
        )
        return 2
    min_sessions = args.min_sessions if args.min_sessions is not None else config.min_sessions

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(f"claudeglass compare: no matching project directories under {root}", file=sys.stderr)
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(f"claudeglass compare: no sessions found under {root} for window {window!r}", file=sys.stderr)
        return 1

    snaps = snapshots.load_snapshots(config_dir) or None

    try:
        session_overrides = load_session_overrides(config_dir)
    except ConfigError as exc:
        print(f"claudeglass compare: {exc}", file=sys.stderr)
        return 2
    session_overrides, _ratings = _merge_dashboard_marks(config_dir, session_overrides)

    # Fewer than min_sessions in either arm is not an error (plan's
    # minimum-sample gate is a caveat on the reading, not a reason to
    # refuse to show data): compare() always returns a full overview with
    # sample_ok="no" on every row instead, and the CLI still exits 0.
    section = compare_mod.compare(
        corpus,
        rates,
        config,
        arm_a=arm_a,
        arm_b=arm_b,
        stratify_by=stratify_by,
        min_sessions=min_sessions,
        snapshots=snaps,
        session_overrides=session_overrides,
        config_dir=config_dir,
    )

    coverage = PricingCoverage()
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in [bundle.top, *bundle.subs]:
            for turn in _priced_turns(tr):
                coverage.add(turn, price_turn(turn, rates.resolve_model(turn.model)))

    model = _wrap_section_as_report(
        section, rates, config, tuple(p.name for p in project_dirs), window, coverage.coverage_pct
    )
    _emit_report_outputs(model, args)
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from . import reconcile as reconcile_mod
    from .discovery import _resolve_window
    from .report import _report_units

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    try:
        admin_result = reconcile_mod.parse_admin_csv(args.admin_csv)
    except reconcile_mod.ReconcileError as exc:
        print(f"claudeglass reconcile: {exc}", file=sys.stderr)
        return 2

    by = tuple(args.by.split(","))

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(f"claudeglass reconcile: no matching project directories under {root}", file=sys.stderr)
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(f"claudeglass reconcile: no sessions found under {root} for window {window!r}", file=sys.stderr)
        return 1

    since_dt, until_dt = _resolve_window(args.days, args.since, args.until)
    since = since_dt.astimezone(timezone.utc).date().isoformat() if since_dt else None
    until = until_dt.astimezone(timezone.utc).date().isoformat() if until_dt else None

    section = reconcile_mod.reconcile(
        corpus,
        rates,
        config,
        admin_rows=admin_result.rows,
        unmapped_headers=admin_result.unmapped_headers,
        by=by,
        since=since,
        until=until,
        config_dir=config_dir,
        units=_report_units(corpus, rates, config, config_dir),
    )

    coverage = PricingCoverage()
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in [bundle.top, *bundle.subs]:
            for turn in _priced_turns(tr):
                coverage.add(turn, price_turn(turn, rates.resolve_model(turn.model)))

    model = _wrap_section_as_report(
        section, rates, config, tuple(p.name for p in project_dirs), window, coverage.coverage_pct
    )
    _emit_report_outputs(model, args)
    # Exit 0 whenever the admin CSV parsed, regardless of how large the
    # reconciliation deltas turn out to be -- only a parse failure (caught
    # above) is a bad-input exit.
    return 0


# -- log-usage ---------------------------------------------------------------


def _cmd_log_usage(args: argparse.Namespace) -> int:
    config_dir = _resolve_config_dir(args.config_dir)
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # pragma: no cover - stdin failures are rare
        print(f"claudeglass log-usage: cannot read stdin: {exc}", file=sys.stderr)
        return 2

    rows = log_usage_mod.parse_usage_json(raw)
    if not rows:
        print(
            "claudeglass log-usage: no usage rows found in the given JSON",
            file=sys.stderr,
        )
        return 2

    csv_path = log_usage_mod.default_usage_log_path(config_dir)
    written = log_usage_mod.append_rows(csv_path, rows, source="manual")
    print(f"Logged {written} row(s) to {csv_path}")
    return 0


# -- probe --------------------------------------------------------------------


def _discover_probe_paths(project_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for project_dir in project_dirs:
        paths.extend(sorted(project_dir.glob("*.jsonl")))
        for session_dir in sorted(p for p in project_dir.iterdir() if p.is_dir()):
            subagents_dir = session_dir / "subagents"
            if subagents_dir.is_dir():
                paths.extend(sorted(subagents_dir.glob("*.jsonl")))
    return paths


def _cmd_probe(args: argparse.Namespace) -> int:
    if args.file:
        target = Path(args.file)
        if not target.exists():
            print(f"claudeglass probe: file not found: {args.file}", file=sys.stderr)
            return 2
        paths = [target]
    else:
        config_dir = _resolve_config_dir(args.config_dir)
        try:
            config = load_config(config_dir)
        except ConfigError as exc:
            print(f"claudeglass probe: {exc}", file=sys.stderr)
            return 2

        root, project_dirs = _resolve_project_dirs_for_args(args, config)
        if not project_dirs:
            print(
                f"claudeglass probe: no matching project directories under {root}",
                file=sys.stderr,
            )
            return 1

        paths = _discover_probe_paths(project_dirs)
        if not paths:
            print(
                f"claudeglass probe: no transcript files found under {root}",
                file=sys.stderr,
            )
            return 1

    result = probe_mod.probe_paths(paths)
    print(probe_mod.render_probe(result))
    return 0


# -- statusline / scrub-fixture (thin delegates) -----------------------------


def _cmd_statusline(args: argparse.Namespace) -> int:
    forward = ["--print-install-fragment"] if getattr(args, "print_install_fragment", False) else []
    # --config-dir was accepted by argparse (it's a "common" flag added to
    # every subcommand, including statusline -- see --help) but silently
    # dropped on the floor here: statusline_mod.main() always resolved
    # against log_usage.resolve_config_dir(None), i.e. $CLAUDE_CONFIG_DIR
    # or ~/.claude/claudeglass, never the value the caller actually passed.
    # Forward it so statusline honours the same --config-dir contract
    # every other subcommand does (fixed for v0.2 release verification).
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        forward += ["--config-dir", str(config_dir)]
    return statusline_mod.main(forward)


# -- export / monthly-report (S1-exports) ------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    from . import exports as exports_mod

    command = "export"
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    window = _window_description(args)
    if not project_dirs:
        print(
            f"claudeglass {command}: no matching project directories under {root}",
            file=sys.stderr,
        )
        return 1

    corpus = _load_corpus_for_args(args, config, config_dir, project_dirs)
    if not corpus.sessions:
        print(
            f"claudeglass {command}: no sessions found under {root} for window {window!r}",
            file=sys.stderr,
        )
        return 1

    if args.aggregate:
        from . import team as team_mod

        if args.aggregate_only is not None or args.hash_slugs is not None:
            print(
                f"claudeglass {command}: --aggregate-only/--per-session and "
                "--hash-slugs/--no-hash-slugs have no effect with --aggregate (a "
                "team document is aggregate-only and hashes project slugs by "
                "construction)",
                file=sys.stderr,
            )
        snaps = snapshots.load_snapshots(config_dir) or None
        document = team_mod.build_team_aggregate(
            corpus,
            rates,
            config,
            config_dir,
            window=window,
            projects=tuple(p.name for p in project_dirs),
            include_projects=args.include_projects,
            snapshots=snaps,
            generated_at=_resolve_generated_at(args),
        )
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    options = exports_mod.resolve_export_options(args.format, args.aggregate_only, args.hash_slugs)

    # Nit 18: --per-session/--hash-slugs are silently accepted but moot
    # for otel-jsonl (it carries no project/session dimension at all --
    # see exports.py's module docstring), so a caller who explicitly
    # asked for per-session data doesn't silently get an aggregate file.
    if options.fmt == "otel-jsonl" and (args.aggregate_only is not None or args.hash_slugs is not None):
        print(
            f"claudeglass {command}: --aggregate-only/--per-session and "
            "--hash-slugs/--no-hash-slugs have no effect on --format otel-jsonl "
            "(it carries no project/session attribute at all)",
            file=sys.stderr,
        )

    # Fix for review finding 2: the hashing opt-out still redacts the
    # OS-username segment (exports._apply_slug_redaction), but the rest
    # of the project slug -- directory shape, a client/codename -- is
    # still visible, so name that residual risk explicitly.
    if not options.hash_slugs and options.fmt != "otel-jsonl":
        print(
            f"claudeglass {command}: --no-hash-slugs is in effect -- project "
            "slugs will still have their OS-username segment redacted, but the "
            "rest of the path shape (which can itself name a client or a "
            "project) is exported as-is",
            file=sys.stderr,
        )

    # Nit 19: build_export_text already accepted a generated_at override
    # (for a reproducible --format json), but _cmd_export never passed
    # anything through, so it was unreachable from the CLI. --generated-at
    # wins; else SOURCE_DATE_EPOCH (the same env var reproducible-build
    # tooling already looks for), interpreted as an integer Unix
    # timestamp; else the default (the current instant) -- see
    # _resolve_generated_at, shared with _cmd_monthly_report (finding 11).
    generated_at = _resolve_generated_at(args)

    text = exports_mod.build_export_text(
        corpus, rates, config, config_dir, options, window=window, generated_at=generated_at
    )

    # Fix for review finding 1 (blocking): csv.DictWriter's default
    # dialect already terminates rows with "\r\n" (render_csv_flat builds
    # the text with an io.StringIO opened with newline="\n", so that
    # "\r\n" survives into the returned string literally). Handing that
    # text to a text-mode writer with the platform's own newline
    # translation switched on then rewrites every "\n" to os.linesep a
    # *second* time -- "\r\n" becomes "\r\r\n" on Windows, and Python's
    # own csv.reader then sees a blank record after every real one. Both
    # output paths below write with newline translation switched off
    # ("newline=''", the same convention render/csv_out.py already uses)
    # so the bytes generated are the bytes written, once.
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(newline="")
            except Exception:
                pass
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _cmd_monthly_report(args: argparse.Namespace) -> int:
    from . import monthly as monthly_mod

    command = "monthly-report"
    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    if not project_dirs:
        print(f"claudeglass {command}: no matching project directories under {root}", file=sys.stderr)
        return 1

    try:
        # Fix for review finding 12: resolve_month's "previous calendar
        # month" default takes its reference date from config.tz (falling
        # back to the machine's own zone when tz is None/unresolvable).
        month = monthly_mod.resolve_month(args.month, config.tz)
    except ValueError as exc:
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return 2

    # Shared with serve --monthly-report (service/monthly_job.py):
    # the no-projects/no-sessions checks, the usage log (review finding
    # 9) and the empty-month note (nit 17). A fixed generated_at (review
    # finding 11) makes repeated runs byte-identical -- see
    # _resolve_generated_at (shared with _cmd_export).
    try:
        paths = monthly_mod.run_monthly_report(
            config=config,
            pricing=rates,
            config_dir=config_dir,
            root=root,
            project_dirs=project_dirs,
            month=month,
            out_dir=Path(args.out),
            load_corpus=lambda dirs: _load_corpus_for_args(args, config, config_dir, dirs),
            generated_at=_resolve_generated_at(args),
            note=lambda text: print(f"claudeglass {command}: {text} (exit 0)", file=sys.stderr),
        )
    except monthly_mod.MonthlyReportError as exc:
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return exc.exit_code
    for path in paths:
        print(str(path))
    return 0


def _cmd_scrub_fixture(args: argparse.Namespace) -> int:
    forward: list[str] = []
    if args.verify:
        forward += ["--verify", args.verify]
    else:
        if not args.session_dir or not args.out:
            print(
                "claudeglass scrub-fixture: --session-dir and --out are required "
                "unless --verify is given",
                file=sys.stderr,
            )
            return 2
        forward += ["--session-dir", args.session_dir, "--out", args.out]
    if args.key_seed:
        forward += ["--key-seed", args.key_seed]
    return scrub_mod.main(forward)


# -- snapshot-config (WP7, unchanged) ----------------------------------------


def _load_snapshot_hook_module():
    """Dynamically import the packaged ``hooks/snapshot-config.py`` module
    from its source text. That script is standalone stdlib and must never
    import from this package (see its own docstring), so the dependency
    runs the other way: this CLI command loads it, rather than it
    importing anything here.

    Reads the source via ``importlib.resources`` (``Traversable.
    read_text``) and ``exec``s it into a fresh module, rather than
    ``importlib.util.spec_from_file_location`` on the resource path
    directly -- fix (surfaced once ``init``, not just ``snapshot-
    config``/``probe-config``, started calling this at runtime): inside
    a zipapp build (``scripts/build-pyz.py``,
    ``tests/test_service_build_pyz.py``), ``importlib.resources.files``
    returns a ``zipfile.Path``, which satisfies the ``Traversable``
    protocol but not ``os.PathLike`` -- ``spec_from_file_location``
    rejects it outright (``TypeError: expected str, bytes or
    os.PathLike object, not Path``). Reading the text and ``exec``-ing
    it works identically on a normal filesystem install and inside a
    zip.
    """
    hook_resource = importlib.resources.files("claudeglass") / "hooks" / "snapshot-config.py"
    try:
        source = hook_resource.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read snapshot-config hook at {hook_resource}: {exc}") from exc
    module = types.ModuleType("_claudeglass_snapshot_hook")
    module.__file__ = str(hook_resource)
    code = compile(source, str(hook_resource), "exec")
    exec(code, module.__dict__)
    # The script's own install_hook copies its __file__, which is not a
    # real file inside a zipapp; copy it out of the package instead.
    module.install_hook = lambda config_dir: hook_health.install_hook_files(config_dir, (hook_health.HOOK_SCRIPT_NAME,))[0]
    return module


def _cmd_snapshot_config(args: argparse.Namespace) -> int:
    hook = _load_snapshot_hook_module()
    config_dir = hook.resolve_config_dir(args.config_dir)

    if args.print_hook:
        print(hook.hook_fragment_text(script=config_dir / "hooks" / "snapshot-config.py"))
        return 0

    if args.install_hook:
        dest = hook.install_hook(config_dir)
        print(f"Installed snapshot-config hook to {dest}")
        print(
            "This does not edit settings.json - add the SessionStart fragment "
            "from --print-hook yourself."
        )
        return 0

    cwd = getattr(args, "project_dir", None) or os.getcwd()
    path, written = hook.snapshot_and_get_path(
        config_dir, cwd, min_interval=args.min_interval, managed_path=args.managed_path
    )
    if path is None:
        print("No snapshot written and none exists yet.", file=sys.stderr)
        return 1
    # Fix #13: previously discarded `written`, so running this command
    # twice inside --min-interval printed an old snapshot's path and
    # exited 0 -- indistinguishable from having actually captured.
    if written:
        print(path)
    else:
        print(f"{path} (unchanged, not rewritten)")
    return 0


# -- probe-config (schema 2) --------------------------------------------------


def _render_probe_config_markdown(snapshot: dict) -> str:
    """The layers + effective-config tables as Markdown -- no paths, only
    hashes and the project slug (schema 2's own privacy posture; see
    ``hooks/snapshot-config.py``'s module docstring).
    """
    snap = snapshots.Snapshot(path=Path("-"), ts=str(snapshot.get("ts", "")), data=snapshot)
    project_slug = snapshot.get("project_slug") or "(unknown project)"

    lines: list[str] = [f"# Config probe: {project_slug}", ""]

    lines += ["## Settings layers", "", "| Layer | Present | Content hash |", "| --- | --- | --- |"]
    layer_map = snapshots.layers(snap)
    for layer_name in snapshots.SETTINGS_LAYER_NAMES:
        layer_info = layer_map.get(layer_name) or {}
        present = "yes" if layer_info.get("present") else "no"
        content_hash = layer_info.get("content_hash") or "-"
        lines.append(f"| {layer_name} | {present} | {content_hash} |")
    lines.append("")

    lines += ["## Effective config", "", "| Key | Value | Source layer |", "| --- | --- | --- |"]
    effective = snapshots.effective_config(snap)
    provenance = snapshots.effective_provenance(snap)
    for key in sorted(effective):
        lines.append(f"| {key} | {effective[key]} | {provenance.get(key, '')} |")
    if not effective:
        lines.append("| *(no settings layer defines any allowlisted key)* | | |")
    lines.append("")

    return "\n".join(lines)


def _cmd_probe_config(args: argparse.Namespace) -> int:
    """Run the hook's own scan for a project directory without a session
    and without writing anything (schema 2, plan "Configuration layers"
    section): ``snapshot-config`` captures and persists; this only prints.
    """
    hook = _load_snapshot_hook_module()
    config_dir = hook.resolve_config_dir(args.config_dir)
    project_path = getattr(args, "project_dir", None) or os.getcwd()
    snapshot = hook.build_snapshot({}, project_path, config_dir, managed_path=args.managed_path)
    print(_render_probe_config_markdown(snapshot))
    return 0


# -- pricing-check (WP2, unchanged) ------------------------------------------


def _cmd_pricing_check(args: argparse.Namespace) -> int:
    """``pricing-check``: print the resolved rate card's provenance and
    rate table, and (with ``--models``) how each given model id resolves
    against it. Exit 2 on a malformed or unreadable pricing file.
    """
    try:
        rates = load_pricing(path=args.pricing, config_dir=args.config_dir)
    except PricingError as exc:
        print(f"claudeglass pricing-check: {exc}", file=sys.stderr)
        return 2

    print(f"Pricing file: {rates.path}")
    print(f"Version:      {rates.version}")
    print(f"SHA8:         {rates.sha8}")
    print(f"Currency:     {rates.currency}")
    if rates.source_url:
        print(f"Source:       {rates.source_url}")
    if rates.retrieved:
        print(f"Retrieved:    {rates.retrieved}")
    print()

    _print_table(rates.describe(), rates.currency)

    if args.models:
        print()
        print("Model resolution:")
        for model_id in (m.strip() for m in args.models.split(",")):
            if not model_id:
                continue
            resolved = rates.resolve_model(model_id)
            if resolved is None:
                print(f"  {model_id} -> UNKNOWN (no matching rate)")
            else:
                approx = " (closest match, not this model's own rate)" if resolved.approximate else ""
                print(
                    f"  {model_id} -> {resolved.canonical_id} (matched via {resolved.matched_via}){approx}"
                )

    return 0


# -- init / baseline (v0.3) / serve (v0.2) -----------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    """``init`` (:mod:`setup_flow`): a few questions, one review of every
    change, then the changes and a summary. ``onboarding.py`` and
    ``setup_flow.py`` never import this module: the snapshot hook (a
    dynamic import of the packaged script), the commands settings.json
    runs and the capture and feedback questions reach them as
    :class:`setup_flow.Tools`.
    """

    options, tools = _setup_flow_inputs(args)
    # sys.stdin/sys.stdout looked up here, on every call, rather than
    # bound as default arguments: pytest's capsys swaps sys.stdout per
    # test, and a default bound at import time would miss it.
    return setup_flow.run(options, tools, stdin=sys.stdin, stdout=sys.stdout)


def _setup_flow_inputs(args: argparse.Namespace) -> tuple[setup_flow.Options, setup_flow.Tools]:
    """``init``'s flags as :class:`setup_flow.Options`, and what the flow
    needs from here as :class:`setup_flow.Tools`. Builds command strings
    only; writes nothing."""
    config_dir = _resolve_config_dir(args.config_dir)
    service_roots = _service_projects_roots(args)
    claude_root = _resolve_claude_root(args.claude_root)
    extra_args = _config_dir_args(config_dir)
    hook = _load_snapshot_hook_module()
    options = setup_flow.Options(
        config_dir=config_dir,
        projects_roots=service_roots,
        claude_root=claude_root,
        answers_path=args.answers,
        non_interactive=args.non_interactive,
        advanced=args.advanced,
        no_install=args.no_install,
        connect=args.connect,
        install_service=args.install_service,
        no_service=args.no_service,
        dry_run=args.dry_run,
        repair_hook=args.repair_hook,
        capture_level=args.capture_level,
        feedback=args.feedback,
        # Fix S6: the shared --all-projects/--project/--project-family
        # flags pick what the first baseline reads.
        all_projects=args.all_projects,
        project=args.project,
        project_family=args.project_family,
    )
    tools = setup_flow.Tools(
        hook_command=hook.hook_command(
            script=Path(config_dir).resolve() / "hooks" / hook_health.HOOK_SCRIPT_NAME, extra_args=extra_args
        ),
        install_hook=hook.install_hook,
        statusline_command=statusline_mod.install_command(extra_args=extra_args),
        capture_commands=_capture_hook_commands(config_dir),
        capture_choice=lambda stdin, stdout, now: _init_capture_choice(
            args, config_dir=config_dir, stdin=stdin, stdout=stdout, now=now
        ),
        feedback_choice=lambda stdin, stdout: _init_feedback_choice(
            args, config_dir=config_dir, claude_root=claude_root, stdin=stdin, stdout=stdout
        ),
        find_wsl_roots=discovery.find_wsl_projects_roots,
    )
    return options, tools


def _init_capture_choice(
    args: argparse.Namespace, *, config_dir: Path, stdin, stdout, now: datetime
) -> tuple[str, str | None] | None:
    """``init``'s full metrics capture question (:func:`onboarding.
    ask_capture_level`, which warns that capture uses tokens and shows
    what each level would have cost over your last
    :data:`CAPTURE_HISTORY_DAYS` days) and its time-box question, asked
    or taken from ``--capture-level``/``--capture-for``/
    ``--capture-no-limit`` and the answers file: the ``(level, until)``
    to save, or ``None`` to leave capture as it is (said on ``stdout``).
    Capture already on is left as it is unless ``--capture-level`` or the
    answers file names a level. Raises :class:`onboarding.OnboardingError`
    for a ``--capture-for`` it can't use. Writes nothing."""
    try:
        config = load_config(config_dir)
        given = onboarding.capture_answer(args.answers, args.capture_level)
    except (ConfigError, onboarding.OnboardingError) as exc:
        stdout.write(f"Metrics capture: skipped ({exc}).\n")
        return None
    # A flag init can't act on stops it before anything is read or
    # written.
    hours = None
    if args.capture_for and args.capture_no_limit:
        raise onboarding.OnboardingError("--capture-for and --capture-no-limit can't both be given.")
    if args.capture_for:
        match = _DURATION_RE.fullmatch(args.capture_for)
        if not match or int(match.group(1)) == 0:
            raise onboarding.OnboardingError(
                f"--capture-for {args.capture_for!r}: use a number and h, d or w, such as 12h, 7d or 2w."
            )
        hours = int(match.group(1)) * _DURATION_UNIT_HOURS[match.group(2).lower()]
    if config.capture.is_on and given is None:
        stdout.write(
            f"\nMetrics capture is {capture_view.describe(config.capture)}. "
            "'claudeglass capture' shows what it costs and changes it.\n"
        )
        return None

    def estimates() -> list[str]:
        if args.dry_run:
            # Reading history keeps its digest cache and salt in the
            # config folder, as every command that reads it does; a dry
            # run writes nothing.
            return []
        past, units = _capture_history(args, config, config_dir)
        return _capture_estimate_lines(past, units) if past is not None else []

    level, notes = onboarding.ask_capture_level(
        estimates=estimates,
        preset=given,
        non_interactive=args.non_interactive,
        stdin=stdin,
        stdout=stdout,
    )
    for note in notes:
        stdout.write(f"(derived) {note}\n")
    if level not in capture_catalogue.LEVELS:
        stdout.write(
            f"{level!r} isn't a level, so metrics capture is left as it is. "
            "'claudeglass capture on --level LEVEL' turns it on.\n"
        )
        return None
    if level == "off" and not config.capture.is_on:
        if not notes:
            stdout.write("Metrics capture left off.\n")
        return None
    until = None
    if level != "off":
        if hours is not None:
            until = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
        else:
            until, timebox_notes = onboarding.ask_capture_until(
                now=now,
                preset=True if args.capture_no_limit else None,
                answers_path=args.answers,
                non_interactive=args.non_interactive,
                stdin=stdin,
                stdout=stdout,
            )
            for note in timebox_notes:
                stdout.write(f"(derived) {note}\n")
    return level, until


def _init_feedback_choice(
    args: argparse.Namespace, *, config_dir: Path, claude_root: Path, stdin, stdout
) -> tuple[bool, bool, list[str]] | None:
    """``init``'s feedback question (:func:`onboarding.ask_feedback`):
    the ``/tl-feedback`` skill and its status-line reminder, asked or
    taken from ``--feedback`` and the answers file. Returns ``(on,
    was_on, derived notes)``, or ``None`` when there's nothing to decide
    (said on ``stdout``). Feedback on in config.toml without its skill
    file (picking Deep turns it on) returns on, so the flow writes the
    skill. Writes nothing."""
    try:
        config = load_config(config_dir)
        given = onboarding.feedback_answer(args.answers, getattr(args, "feedback", None))
    except (ConfigError, onboarding.OnboardingError) as exc:
        stdout.write(f"Feedback: skipped ({exc}).\n")
        return None
    current = config.capture
    was_on = "feedback_skill" in current.feedback
    if was_on and given is None:
        from . import footprint

        if footprint.read_feedback_skill(claude_root) == capture_catalogue.feedback_skill_text():
            stdout.write("\nThe /tl-feedback skill is on. 'claudeglass capture feedback off' turns it off.\n")
            return None
        # On in config.toml without its skill file: picking Deep just
        # turned it on (config.set_capture), or the file went missing.
        stdout.write(
            "\nThe /tl-feedback survey is on"
            + (", as part of Deep" if current.level == "deep" else "")
            + ". 'claudeglass capture feedback off' turns it off.\n"
        )
        return True, was_on, []
    on, notes = onboarding.ask_feedback(preset=given, non_interactive=args.non_interactive, stdin=stdin, stdout=stdout)
    for note in notes:
        stdout.write(f"(derived) {note}\n")
    return on, was_on, notes


#: Short pause (seconds) before probing a just-installed service, so a
#: platform that starts it immediately on install (systemd's
#: ``enable --now``, launchd's ``bootstrap`` with ``RunAtLoad``) has a
#: moment to actually come up before ``/api/health`` is hit (``serve``
#: binds its port before its first scan, so a few seconds at most).
_POST_INSTALL_PROBE_DELAY_S = 1.0


#: Moved to :mod:`installer` (``setup_status`` needs them too, and
#: can't import this module); the old names stay for callers here.
_http_health_ok = installer_mod.http_health_ok
_http_health_version = installer_mod.http_health_version


def _probe_service_after_install(
    platform: str,
    *,
    bind: str = "127.0.0.1",
    port: int = 8765,
    is_registered_fn=None,
    health_check=None,
    sleep_fn=None,
    version_check=None,
) -> None:
    """Print a short "is it actually working" summary right after
    :func:`installer.install` returns: :func:`installer.is_registered`
    (per-platform probe) and one ``/api/health`` hit. Every dependency
    is an injectable keyword-only parameter (default: the real thing)
    purely so tests never have to monkeypatch ``time.sleep``/spawn a
    real HTTP server/shell out to ``schtasks``/``systemctl``/
    ``launchctl`` to exercise this function.
    """
    import time as time_mod

    is_registered_fn = is_registered_fn or (lambda: installer_mod.is_registered(platform))
    if version_check is None:
        # Only the real health check has a real dashboard to ask.
        version_check = _http_health_version if health_check is None else (lambda url: None)
    health_check = health_check or _http_health_ok
    sleep_fn = sleep_fn or time_mod.sleep

    sleep_fn(_POST_INSTALL_PROBE_DELAY_S)

    registered = is_registered_fn()
    if registered is True:
        print("claudeglass: confirmed -- the service is registered to start at logon.")
    elif registered is False:
        print("claudeglass: the service registration could not be confirmed -- check the output above.")
    else:
        print("claudeglass: service registration status could not be determined on this platform.")

    url = f"http://{bind}:{port}"
    if health_check(url):
        from . import __version__

        answering = version_check(url)
        if answering is not None and answering != __version__:
            print(
                f"claudeglass: {url} is answering with version {answering}, not {__version__}: "
                "an older copy still holds the port. See 'An old dashboard won't go away' "
                f"({OLD_DASHBOARD_HELP}) to find and stop it."
            )
        else:
            print(f"claudeglass: the service is already responding at {url}")
    else:
        print(
            f"claudeglass: not responding yet (it can take a few seconds to start). "
            f"Once it's running, open {url}; it shows its progress while it reads your history."
        )


def _cmd_install_service(args: argparse.Namespace) -> int:
    """``install-service`` (v3): register ``serve`` to start at
    logon/boot, for anyone who skipped it during ``init`` (or ran
    ``init`` before this milestone existed).
    """
    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)

    try:
        plan = installer_mod.plan_service_install(
            sys.executable, projects_root_path, config_dir, port=args.port, bind=args.bind
        )
        installer_mod.install(plan, dry_run=args.dry_run)
    except installer_mod.InstallerError as exc:
        print(f"claudeglass install-service: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        _probe_service_after_install(plan.platform, bind=args.bind, port=args.port)

    return 0


#: What ``update`` installs from by default.
UPDATE_SOURCE = "git+https://github.com/PaulMorrisDev/claudeglass"

#: The troubleshooting section that finds and stops an older dashboard
#: holding the port, for the messages that send you there.
OLD_DASHBOARD_HELP = (
    "https://github.com/PaulMorrisDev/claudeglass/blob/main/docs/first-run.md#an-old-dashboard-wont-go-away"
)

#: Where a ``.pyz`` user downloads the newest copy.
_RELEASES_URL = "https://github.com/PaulMorrisDev/claudeglass/releases/latest"


def _cmd_update(args: argparse.Namespace, *, runner=None) -> int:
    """``update``: install the newest version with pip
    (``--force-reinstall``, because pip skips a copy whose version number
    hasn't changed), then hand over to it. ``update --finish`` runs with
    the *new* code, so every step after the install is the new version's
    own (:func:`_cmd_update_finish`)."""
    import subprocess

    from . import __version__

    runner = runner or subprocess.run
    if installer_mod.detect_pyz_path() is not None:
        print(
            "claudeglass update: this copy runs from a .pyz file, which pip can't update. "
            f"Download the new claudeglass.pyz from {_RELEASES_URL}, put it in place of this one, "
            f"then run '{invocation.command_prefix()} install-service'.",
            file=sys.stderr,
        )
        return 2

    install = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", args.source]
    finish = [sys.executable, "-m", "claudeglass", "update", "--finish", *_update_finish_args(args)]
    print(f"claudeglass update: this is version {__version__}.")
    print("1. Install the newest version:\n   " + invocation.shell_line(install))
    if args.dry_run:
        print(
            "2. Finish with the new version: restart the dashboard on it, bring Claude Code's hook entries "
            "up to date, and look for copies installed for other Pythons:\n   " + invocation.shell_line(finish)
        )
        print("Dry run: nothing installed or restarted.")
        return 0

    if runner(install).returncode != 0:
        print(
            "claudeglass update: pip could not install the new version (its message is above). "
            "Nothing else was changed.",
            file=sys.stderr,
        )
        return 1
    probe = runner(
        [sys.executable, "-c", "import claudeglass; print(claudeglass.__version__)"],
        capture_output=True,
        text=True,
    )
    new_version = (probe.stdout or "").strip() or "unknown"
    print(f"   Installed version {new_version}.")
    print("2. Finish with the new version:\n   " + invocation.shell_line(finish))
    sys.stdout.flush()
    return runner(finish).returncode


def _update_finish_args(args: argparse.Namespace) -> list[str]:
    """The flags ``update`` passes on to ``update --finish``."""
    out = [
        *(arg for root in _service_projects_roots(args) for arg in ("--projects-root", str(root))),
        "--config-dir",
        str(_resolve_config_dir(args.config_dir)),
        "--port",
        str(args.port),
        "--bind",
        args.bind,
    ]
    if args.claude_root:
        out += ["--claude-root", str(_resolve_claude_root(args.claude_root))]
    if args.no_service:
        out.append("--no-service")
    if args.yes:
        out.append("--yes")
    return out


def _cmd_update_finish(
    args: argparse.Namespace,
    *,
    stdin=None,
    stdout=None,
    runner=None,
    is_registered_fn=None,
    install_service_fn=None,
    health_version_fn=None,
    registered_python_fn=None,
    copies_fn=None,
    port_holder_fn=None,
    now: datetime | None = None,
) -> int:
    """``update --finish``: the rest of an update, run by the version just
    installed. ``update`` runs it itself; after an update from 0.6.0 or
    older, whose ``update`` doesn't, run it by hand.

    1. Refresh the hook files this version changed.
    2. Restart the dashboard on this Python (``install-service``, which
       also points the logon service here) when it starts at logon. On
       Windows, an older dashboard started by hand that still holds the
       port is named and, after a yes, stopped.
    3. Bring Claude Code's settings.json up to date: a SessionStart hook
       command that can't run, the entries capture needs, and this tool's
       statusline when it runs another Python. Each change is shown and
       made after a yes; settings.json is backed up first.
    4. Name copies of this tool installed for other Pythons and, once the
       dashboard runs the new version and nothing else needs them, remove
       them after a yes.

    ``--yes`` answers yes throughout; ``--dry-run`` shows and asks nothing.
    Returns 2 when the dashboard couldn't be registered, 1 when an older
    dashboard still answers on the port, else 0."""
    import subprocess

    from . import __version__, upgrade

    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    runner = runner or subprocess.run
    windows = installer_mod.detect_platform() == "windows"
    is_registered_fn = is_registered_fn or installer_mod.is_registered
    install_service_fn = install_service_fn or _cmd_install_service
    health_version_fn = health_version_fn or _http_health_version
    registered_python_fn = registered_python_fn or (lambda: installer_mod.registered_python(runner=runner))
    port_holder_fn = port_holder_fn or (lambda port: upgrade.port_holder(port, runner=runner) if windows else None)
    copies_fn = copies_fn or (
        lambda also: upgrade.other_copies(
            upgrade.candidate_pythons(also=also, runner=runner, windows=windows), runner=runner
        )
    )
    config_dir = _resolve_config_dir(args.config_dir)
    claude_root = _resolve_claude_root(args.claude_root)
    command = invocation.command_prefix()
    url = f"http://{args.bind}:{args.port}"

    def ask(question: str) -> bool:
        if args.dry_run:
            return False
        if args.yes:
            stdout.write(f"{question} yes (--yes)\n")
            return True
        stdout.write(f"{question} (y/n) [n]: ")
        stdout.flush()
        return (stdin.readline() or "").strip().lower() in ("y", "yes")

    stdout.write(f"claudeglass update: finishing with version {__version__}, installed for {sys.executable}.\n")
    ran_from = registered_python_fn()

    if not args.dry_run:
        try:
            refreshed = hook_health.refresh_hook_files(config_dir)
        except OSError:
            refreshed = []
        if refreshed:
            stdout.write(f"Refreshed {len(refreshed)} hook file{'s' if len(refreshed) != 1 else ''} this version changed.\n")

    # -- the dashboard ---------------------------------------------------
    status = 0
    registered = None
    if args.no_service:
        stdout.write("Left the dashboard alone (--no-service).\n")
    else:
        registered = is_registered_fn()
        if registered is False:
            stdout.write(
                "The dashboard isn't set to start at logon, so there is nothing to restart. "
                f"Start it with '{command} serve', or have it start at logon with '{command} install-service'.\n"
            )
        else:
            stdout.flush()
            if install_service_fn(args) != 0:
                status = 2
    answering = None if args.dry_run else health_version_fn(url)
    if answering not in (None, __version__):
        holder = port_holder_fn(args.port)
        if holder is not None and upgrade.is_python(holder[1]):
            pid, program = holder
            stdout.write(
                f"An older dashboard (version {answering}) still holds port {args.port}: process {pid}, {program}. "
                "The logon service didn't start it, so restarting the service can't replace it.\n"
            )
            if ask("Stop it and start this version?"):
                if not upgrade.stop_process(pid, runner=runner):
                    stdout.write(
                        f"Could not stop process {pid}. See 'An old dashboard won't go away': {OLD_DASHBOARD_HELP}\n"
                    )
                elif registered is False or args.no_service:
                    stdout.write(f"Stopped. Start this version with '{command} serve'.\n")
                    answering = None
                else:
                    stdout.flush()
                    install_service_fn(args)
                    answering = health_version_fn(url)
        elif holder is not None:
            stdout.write(
                f"Port {args.port} is held by {holder[1] or f'process {holder[0]}'}, which isn't a Python: an old "
                f"Docker setup? See 'An old dashboard won't go away': {OLD_DASHBOARD_HELP}\n"
            )
    if answering not in (None, __version__) and registered is not False and not args.no_service:
        status = status or 1

    # -- Claude Code's settings.json -----------------------------------------
    stdout.write("\nClaude Code's settings:\n")
    changed_any = False
    health = hook_health.check(config_dir, claude_root=claude_root)
    if health.fixed_command is not None:
        changed_any = True
        onboarding.offer_hook_repair(
            health, repair_hook=args.yes, non_interactive=args.dry_run, stdin=stdin, stdout=stdout, now=now
        )
    try:
        capture = load_config(config_dir=config_dir).capture
    except ConfigError as exc:
        capture = None
        stdout.write(f"config.toml has a problem, so capture's hook entries weren't checked: {exc}\n")
    if capture is not None and (capture.is_on or capture.coaching_notes_on):
        wanted = hook_health.capture_specs(capture.hook_metrics())
        capture_health = hook_health.check_capture(wanted, claude_root=claude_root, config_dir=config_dir)
        if capture_health.blocked_by is not None:
            stdout.write(f"{hook_health.POLICY_TEXT[capture_health.blocked_by]}\n")
        elif not capture_health.ok:
            changed_any = True
            _capture_settings_step(
                wanted,
                config_dir=config_dir,
                claude_root=claude_root,
                dry_run=args.dry_run,
                assume_yes=args.yes,
                stdin=stdin,
                stdout=stdout,
            )
    statusline_plan = hook_health.plan_statusline_python(sys.executable, claude_root=claude_root)
    if statusline_plan.new_text is not None:
        changed_any = True
        stdout.write(f"\nThis changes {statusline_plan.settings_path}:\n")
        for line in statusline_plan.changes:
            stdout.write(f"- {line}\n")
        stdout.write("\n" + statusline_plan.diff + "\n")
        if ask("Make this change? settings.json is backed up first."):
            try:
                backup = hook_health.connect(statusline_plan)
                stdout.write(f"Done. The previous settings.json is at {backup}\n")
            except (OSError, ValueError) as exc:
                stdout.write(f"Could not change settings.json: {exc}\n")
        elif not args.dry_run:
            stdout.write("Left unchanged.\n")
    if not changed_any:
        stdout.write("Up to date: the hooks and statusline this tool added run as they should.\n")

    # -- copies for other Pythons ----------------------------------------------
    copies = copies_fn([ran_from])
    statusline_runs = hook_health.statusline_python(claude_root)
    # The logon service moved to this Python only if install-service ran
    # and succeeded; until then it still starts the copy it ran before.
    service_moved = registered is not False and not args.no_service and status != 2 and not args.dry_run

    def same_install(a: str | None, b: str) -> bool:
        # python.exe and pythonw.exe of one install sit side by side.
        return bool(a) and os.path.normcase(os.path.dirname(os.path.realpath(a))) == os.path.normcase(
            os.path.dirname(os.path.realpath(b))
        )

    for copy in copies:
        remove_later = f"{invocation._quote(copy.python)} -m pip uninstall claudeglass"
        stdout.write(f"\nAnother copy, version {copy.version}, is installed for {copy.python}.\n")
        if answering not in (None, __version__):
            stdout.write(f"An older dashboard still runs, so it stays for now. Remove it later with: {remove_later}\n")
            continue
        if not service_moved and same_install(ran_from, copy.python):
            stdout.write(
                f"The dashboard still starts from it at logon, so it stays. Remove it after '{command} install-service' "
                f"with: {remove_later}\n"
            )
            continue
        if same_install(statusline_runs, copy.python):
            stdout.write(f"The statusline still runs it, so it stays. Remove it later with: {remove_later}\n")
            continue
        stdout.write("Nothing this tool set up uses it now, and running it shows an older version.\n")
        if ask("Remove it?"):
            if upgrade.remove_copy(copy, runner=runner):
                stdout.write("Removed.\n")
            else:
                stdout.write(f"pip could not remove it (its message is above). Try: {remove_later}\n")
        elif not args.dry_run:
            stdout.write(f"Left installed. Remove it later with: {remove_later}\n")

    if args.dry_run:
        stdout.write("\nDry run: nothing restarted, changed or removed.\n")
    elif status == 0:
        where = f" The dashboard at {url} runs it." if answering == __version__ else ""
        stdout.write(f"\nclaudeglass update: done. Version {__version__} is installed for {sys.executable}.{where}\n")
    elif status == 1:
        stdout.write(
            f"\nclaudeglass update: version {__version__} is installed, but {url} still answers with "
            f"version {answering}. See 'An old dashboard won't go away': {OLD_DASHBOARD_HELP}\n"
        )
    return status


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    """``uninstall-service`` (v3): remove a registration made by
    ``install-service`` or by ``init``'s own logon-service step.
    ``--port``/``--bind`` don't apply here -- removing a registration
    never depends on them (see
    ``installer.InstallPlan.uninstall_commands``/``uninstall_files``).
    """
    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)

    plan = installer_mod.plan_service_install(sys.executable, projects_root_path, config_dir)
    return installer_mod.uninstall(plan, dry_run=args.dry_run)


def _cmd_changes(args: argparse.Namespace) -> int:
    """``changes``: everything this tool has installed or changed on this
    machine, whether it costs tokens, and the command that undoes it
    (``footprint.inventory``)."""
    from . import footprint

    config_dir = _resolve_config_dir(args.config_dir)
    items = footprint.inventory(
        config_dir,
        service_registered=installer_mod.is_registered(),
        claude_root=_resolve_claude_root(getattr(args, "claude_root", None)),
    )
    print("What claudeglass has installed and changed\n")
    for item in items:
        print(f"{item.title}: {item.status}")
        print(f"  Where: {item.where}")
        print(f"  What it does: {pages.plain(item.what_it_does)}")
        print(f"  Tokens: {pages.plain(item.token_cost)}")
        if item.status in ("installed", "in place"):
            print(f"  To undo: {pages.plain(item.undo)}")
        print()
    print("What to expect\n")
    for title, text in footprint.expectations(footprint.capture_setting(config_dir)):
        print(f"- {title}. {pages.plain(text)}")
    print(f"\nTo remove everything: {footprint.UNINSTALL_COMMAND}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """``status``: whether each part of the setup works
    (:func:`setup_status.check_setup`). Exits 1 only when an essential
    part is a problem; waiting and off parts exit 0."""
    from . import setup_status
    from .service.serve import STORE_FILENAME
    from .service.store import read_entrypoint_counts

    config_dir = _resolve_config_dir(args.config_dir)
    items = setup_status.check_setup(
        config_dir,
        _resolve_claude_root(getattr(args, "claude_root", None)),
        entrypoints=read_entrypoint_counts(config_dir / STORE_FILENAME),
    )
    print("ClaudeGlass setup\n")
    for line in setup_status.lines(items):
        print(pages.plain(line))
    print(f"\n{setup_status.verdict(items)}")
    return 1 if setup_status.essential_problem(items) else 0


_DURATION_RE = re.compile(r"\s*(\d+)\s*([hdw])\s*", re.IGNORECASE)
_DURATION_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7}


def _capture_until(args: argparse.Namespace, now: datetime) -> str | None:
    """``--until`` as given, ``--for`` turned into an ISO time, or ``""``
    for ``--no-limit`` (CAP-8: an explicit "no limit", as opposed to
    ``None``, which leaves ``set_capture``'s own default time-box in
    play -- see its docstring). ``None`` when none of the three was
    given. Raises ``ValueError`` for a duration it can't read, or more
    than one of ``--for``/``--until``/``--no-limit`` at once."""
    no_limit = getattr(args, "no_limit", False)
    if sum(bool(x) for x in (args.until, args.for_duration, no_limit)) > 1:
        raise ValueError("give --for, --until or --no-limit, not more than one")
    if no_limit:
        return ""
    if args.until:
        return args.until
    if not args.for_duration:
        return None
    match = _DURATION_RE.fullmatch(args.for_duration)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"--for {args.for_duration!r}: use a number and h, d or w, such as 12h, 7d or 2w")
    hours = int(match.group(1)) * _DURATION_UNIT_HOURS[match.group(2).lower()]
    return (now + timedelta(hours=hours)).isoformat(timespec="seconds")


def _capture_cost_lines(ids) -> list[str]:
    """Plain lines on what ``ids`` add to Claude's context and replies."""
    rough = capture_catalogue.rough_tokens(ids)
    lines = []
    if rough["session_note"]:
        lines.append(f"about {rough['session_note']} tokens of note when a session starts, is cleared or compacts")
    if rough["subagent_note"]:
        lines.append(f"about {rough['subagent_note']} tokens of note when a subagent starts")
    if rough["reply_tag"]:
        lines.append(f"about {rough['reply_tag']} tokens of tag at the end of each reply")
    if rough["report_tag"]:
        lines.append(f"about {rough['report_tag']} tokens of tag at the end of each subagent report")
    if rough["tool_note"]:
        lines.append(f"about {rough['tool_note']} tokens of note after each large or web tool result")
    return lines


def _capture_corpus(args: argparse.Namespace, config: Config, config_dir: Path, *, days=None, since=None) -> Corpus:
    """Every project's sessions over ``days`` or from ``since`` on:
    capture runs in all of them unless ``[capture] projects`` narrows
    it. ``--until`` is the capture end time here, not a window."""
    roots = discovery.projects_roots(args.projects_root, config.extra_projects_roots)
    project_dirs = discovery.resolve_project_dirs(
        roots, slugs=None, all_projects=True, family_regex=None, exclude_projects=config.exclude_projects
    )
    window = argparse.Namespace(**{**vars(args), "days": days, "since": since, "until": None, "limit": None})
    return _load_corpus_for_args(window, config, config_dir, project_dirs)


def _capture_pricing(args: argparse.Namespace, config: Config, config_dir: Path) -> Pricing | None:
    try:
        return load_pricing(path=args.pricing or config.pricing_path, config_dir=config_dir)
    except PricingError:
        return None


def _capture_history(args: argparse.Namespace, config: Config, config_dir: Path):
    """``(history, units)`` from your last :data:`CAPTURE_HISTORY_DAYS`
    days, or ``(None, None)`` when the rate card can't be read."""
    from . import capture
    from .report import _report_units

    rates = _capture_pricing(args, config, config_dir)
    if rates is None:
        return None, None
    corpus = _capture_corpus(args, config, config_dir, days=CAPTURE_HISTORY_DAYS)
    return capture.history(corpus, rates, days=CAPTURE_HISTORY_DAYS), _report_units(corpus, rates, config, config_dir)


def _flatten_corpus(corpus: Corpus) -> list[TranscriptResult]:
    """Every parsed transcript in ``corpus`` (top-level session plus its
    subagents), for a hook_health scan that reads events, not turns."""
    results: list[TranscriptResult] = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        results.extend([bundle.top, *bundle.subs])
    return results


def _scan_hook_errors(args: argparse.Namespace, config: Config, config_dir: Path) -> hook_health.HookErrorHealth:
    """Non-blocking hook errors (SURV-HE) over your last
    :data:`CAPTURE_HISTORY_DAYS` days -- every hook Claude Code ran, not
    only metrics capture's own, so this runs whether or not capture is
    on."""
    corpus = _capture_corpus(args, config, config_dir, days=CAPTURE_HISTORY_DAYS)
    return hook_health.count_hook_errors(_flatten_corpus(corpus))


#: "This week" for :func:`_measure_deep_wait` -- independent of
#: :data:`CAPTURE_HISTORY_DAYS`, which is a cost-estimate window, not a
#: latency one.
_DEEP_WAIT_DAYS = 7


def _measure_deep_wait(args: argparse.Namespace, config: Config, config_dir: Path) -> hook_health.DeepWaitStats:
    """Deep's big_output/web PostToolUse hook's real median/p90 wait
    (CAP-9/F10), over the last :data:`_DEEP_WAIT_DAYS` days -- "this
    week", the same window :meth:`hook_health.DeepWaitStats.summary`
    names."""
    corpus = _capture_corpus(args, config, config_dir, days=_DEEP_WAIT_DAYS)
    return hook_health.measure_deep_wait(_flatten_corpus(corpus))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _capture_estimate_lines(past, units, sample: int = 100) -> list[str]:
    """What each level would have cost over ``past``, one line each."""
    from . import capture

    if not past.sessions:
        return [
            f"No sessions in your last {past.days} days to work out what it would cost. Roughly, at Essentials:",
            *(f"  - {line}" for line in _capture_cost_lines(capture_catalogue.level_metrics("essentials"))),
        ]
    sampled = f", {sample}% of sessions captured" if sample < 100 else ""
    lines = [
        f"What each level would have cost over your last {past.days} days "
        f"({_plural(past.sessions, 'session')}, {_plural(past.subagents, 'subagent')}{sampled}):"
    ]
    for level, est in capture.level_estimates(past, sample).items():
        title = capture_catalogue.LEVEL_TITLES.get(level, level)
        if est.cost <= 0:
            lines.append(f"  {title:<11} nothing: it only logs a few events to a local file")
            continue
        tokens = round((est.note_tokens + est.tag_tokens) * 7 / est.days) if est.days else 0
        share = f", {capture_view.share_text(est.share)} of what you spent" if est.share is not None else ""
        amount = capture_view.amount_text(units, est.per_week, "a week")
        lines.append(f"  {title:<11} about {format_cell(tokens, 'tokens')} tokens and {amount}{share}")
    return lines


def _capture_usage_lines(use, units) -> list[str]:
    """What capture measured since it was turned on."""
    if not use.sessions and not use.subagents:
        return [
            "No captured sessions yet: the note is added to sessions and subagents started after capture was "
            "turned on."
        ]
    since = f" since {use.since[:10]}" if use.since else ""
    share = f", {capture_view.share_text(use.share)} of what those sessions cost" if use.share is not None else ""
    lines = [
        f"Measured{since}: {_plural(use.sessions, 'session')} and {_plural(use.subagents, 'subagent')} captured",
        f"  about {format_cell(use.note_tokens, 'tokens')} tokens of note and {format_cell(use.tag_tokens, 'tokens')} "
        f"tokens of tag: {capture_view.amount_text(units, use.cost)}{share}",
    ]
    if use.coverage is not None:
        reports = (
            f" and {format_cell(use.report_coverage, 'pct')} of agent reports"
            if use.report_coverage is not None
            else ""
        )
        lines.append(f"  Claude tagged {format_cell(use.coverage, 'pct')} of your messages{reports}")
    return lines


def _coaching_usage_lines(use, units) -> list[str]:
    """What coaching notes cost over the last few days."""
    if not use.notes:
        return [f"Coaching notes: none in the last {CAPTURE_HISTORY_DAYS} days."]
    share = f", {capture_view.share_text(use.share)} of what those sessions cost" if use.share is not None else ""
    kinds = ", ".join(f"{kind} {n}" for kind, n in sorted(use.by_kind.items(), key=lambda item: -item[1]))
    return [
        f"Coaching notes in the last {CAPTURE_HISTORY_DAYS} days: {use.notes} in {_plural(use.sessions, 'session')} "
        f"({kinds}), about {format_cell(use.note_tokens, 'tokens')} tokens: "
        f"{capture_view.amount_text(units, use.cost)}{share}"
    ]


def _coaching_measured(args, config: Config, config_dir: Path) -> list[str]:
    from . import capture as capture_mod
    from .report import _report_units

    rates = _capture_pricing(args, config, config_dir)
    if rates is None:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=CAPTURE_HISTORY_DAYS)).isoformat(timespec="seconds")
    corpus = _capture_corpus(args, config, config_dir, days=CAPTURE_HISTORY_DAYS)
    units = _report_units(corpus, rates, config, config_dir)
    return _coaching_usage_lines(capture_mod.coaching_usage(corpus, rates, since=since), units)


def _capture_refresh(args, config: Config, config_dir: Path, *, stdout, now: datetime) -> int:
    """``capture refresh``: work out ``coaching.json`` from your last
    ``coaching.DAYS`` days now, as the dashboard's service does daily."""
    from . import coaching

    rates = _capture_pricing(args, config, config_dir)
    if rates is None:
        stdout.write("The rate card can't be read, so the split points can't be worked out.\n")
        return 2
    corpus = _capture_corpus(args, config, config_dir, days=coaching.DAYS)
    report = build_report(
        corpus,
        rates,
        config,
        projects=tuple(sorted({bundle.slug for bundle in corpus.sessions if bundle.slug})),
        window=f"last {coaching.DAYS} days",
        config_dir=config_dir,
    )
    data = coaching.from_report(report, config_dir, config.thresholds, now=now)
    for line in coaching.describe(data):
        stdout.write(f"{line}\n")
    if args.dry_run:
        stdout.write(f"Dry run: {coaching.path(config_dir)} left unchanged.\n")
    else:
        stdout.write(f"Written to {coaching.write(config_dir, data)}.\n")
    if not config.capture.coaching_notes_on:
        stdout.write("Coaching notes are off, so nothing reads it yet: 'claudeglass capture enable coaching_notes'.\n")
    return 0


def _capture_metric_changes(action: str, values: list[str], current: CaptureConfig) -> dict:
    """``set_capture`` arguments for ``enable``/``disable``. Raises
    ``ValueError`` naming an id it doesn't know."""
    if not values:
        raise ValueError(f"'capture {action}' needs one or more metric ids (see 'claudeglass capture status')")
    known = capture_catalogue.METRICS_BY_ID
    unknown = [v for v in values if v not in known]
    if unknown:
        raise ValueError(f"unknown metric {', '.join(unknown)}; known: {', '.join(known)}")
    derived = [v for v in values if known[v].group == "derived"]
    if derived:
        raise ValueError(f"{', '.join(derived)} {'is' if len(derived) == 1 else 'are'} always measured, from what the transcripts already hold")
    level_ids = [v for v in values if v in capture_catalogue.LEVEL_METRIC_IDS]
    changes: dict = {}
    if level_ids:
        base = [m for m in capture_catalogue.active_metrics(current.level, current.metrics) if m in capture_catalogue.LEVEL_METRIC_IDS]
        if action == "enable":
            chosen = base + [m for m in level_ids if m not in base]
        else:
            dropped = set(level_ids)
            dropped |= {m.id for m in capture_catalogue.METRICS if set(m.requires) & dropped}
            chosen = [m for m in base if m not in dropped]
        changes["metrics"] = chosen
    for key, ids in (("feedback", capture_catalogue.FEEDBACK_IDS), ("coaching", capture_catalogue.COACHING_IDS)):
        picked = [v for v in values if v in ids]
        if picked:
            now_on = list(getattr(current, key))
            changes[key] = now_on + [v for v in picked if v not in now_on] if action == "enable" else [v for v in now_on if v not in picked]
    return changes


def _capture_hook_commands(config_dir: Path) -> dict[str, str | None]:
    """A command per :data:`hook_health.CAPTURE_SCRIPTS`, or ``None``
    (ROB-P9) for one whose path can't be safely written into a command
    string -- the caller's job to refuse the change rather than write a
    broken or unsafe one (:func:`_capture_settings_step`)."""
    hooks_dir = Path(config_dir).resolve() / "hooks"
    extra_args = _config_dir_args(config_dir)
    return {script: hook_health.hook_command(hooks_dir / script, extra_args) for script in hook_health.CAPTURE_SCRIPTS}


def _capture_settings_step(
    wanted: tuple, *, config_dir: Path, claude_root: Path, dry_run: bool, assume_yes: bool, stdin, stdout, removing: bool = False
) -> bool:
    """Show the settings.json change that makes it run exactly the
    capture entries in ``wanted``, and make it after a yes. Installs the
    hook scripts into this tool's folder first, and the salt the free
    signals hash session ids with. Returns False when the change was
    needed but not made."""
    if wanted and not dry_run:
        for script in hook_health.CAPTURE_SCRIPTS:
            hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[script])
        load_or_create_salt(config_dir)
    commands = _capture_hook_commands(config_dir)
    if any(commands[spec.script] is None for spec in wanted):
        # ROB-P9: the Python or a hook script's own path can't be safely
        # written into a command string -- refuse rather than write a
        # broken or unsafe one into settings.json.
        stdout.write(
            f"Could not build a safe capture hook command: {config_dir} or this Python holds a quote, $, "
            "backtick, or is a UNC path, none of which can be written into settings.json safely. Move "
            "claudeglass's data folder somewhere with a plain path, then try again.\n"
        )
        return False
    policy = hook_health.hook_policy(claude_root) if wanted else None
    if policy is not None:
        stdout.write(
            f"{hook_health.POLICY_TEXT[policy]} The entries below can still be written, but Claude Code "
            "won't run them while that holds.\n"
        )
    plan = hook_health.plan_capture(wanted, commands, claude_root=claude_root)
    if plan.new_text is None:
        for line in plan.changes:  # a settings.json it can't read
            stdout.write(f"{line}\n")
        if not plan.changes:
            stdout.write(
                "settings.json already runs the capture hooks these metrics need.\n"
                if wanted
                else "settings.json runs no capture hooks.\n"
            )
        return not plan.changes
    stdout.write(f"\nThis changes {plan.settings_path}:\n")
    for line in plan.changes:
        stdout.write(f"- {line}\n")
    stdout.write("\n" + plan.diff + "\n")
    later = "claudeglass capture remove" if removing else "claudeglass capture connect"
    if dry_run:
        stdout.write(f"Dry run: settings.json left unchanged. Run '{later}' to make it.\n")
        return False
    if not assume_yes:
        stdout.write("Make this change? settings.json is backed up first. (y/n) [n]: ")
        stdout.flush()
        if (stdin.readline() or "").strip().lower() not in ("y", "yes"):
            stdout.write(f"Left unchanged. Run '{later}' to make it later.\n")
            return False
    try:
        backup = hook_health.connect(plan)
    except (OSError, ValueError) as exc:
        stdout.write(f"Could not change settings.json: {exc}\n")
        return False
    stdout.write("Done." + (f" The previous settings.json is at {backup}" if backup else "") + f"\n{RESTART_NOTE}\n")
    return True


def _capture_skill_step(
    want: bool,
    *,
    claude_root: Path,
    dry_run: bool,
    assume_yes: bool,
    stdin,
    stdout,
    skill: str = capture_catalogue.FEEDBACK_SKILL,
) -> bool:
    """Make the skill (``<claude-root>/skills/<skill>/SKILL.md``: by
    default ``/tl-feedback``, or ``/tl-brief``) there or not, as ``want``
    says: show the file to add, the diff, or the file to remove, and make
    the change after a yes. A ``SKILL.md`` there that this tool didn't
    write is left alone. Returns False when a change was needed but not
    made."""
    from . import footprint

    action = next(a for a, (name, _t, _w) in _SKILL_SWITCHES.items() if name == skill)
    path = footprint.skill_path(skill, claude_root)
    now_text = footprint.read_skill(skill, claude_root)
    ours = now_text is not None and footprint.is_own_skill(skill, now_text)
    text = footprint.SKILL_TEXTS[skill]()
    if want:
        later = f"claudeglass capture {action} on"
        if now_text == text:
            stdout.write(f"The /{skill} skill is in place: {path}\n")
            return True
        if now_text is not None and not ours:
            stdout.write(
                f"{path} holds a skill this tool didn't write, so it is left alone. Move it elsewhere, then run "
                f"'{later}'.\n"
            )
            return False
        if now_text is None:
            stdout.write(f"\nThis adds the /{skill} skill, {path}:\n\n")
            stdout.write("".join(f"    {line}\n" if line else "\n" for line in text.splitlines()) + "\n")
            question = "Add it?"
        else:
            import difflib

            diff = "".join(
                difflib.unified_diff(
                    now_text.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile="SKILL.md (now)",
                    tofile="SKILL.md (after)",
                )
            )
            stdout.write(f"\nThis updates the /{skill} skill, {path}:\n\n{diff}\n")
            question = "Update it?"
    else:
        later = f"claudeglass capture {action} off"
        if not ours:
            return True
        stdout.write(f"\nThis removes the /{skill} skill, {path}.\n")
        question = "Remove it?"
    if dry_run:
        stdout.write(f"Dry run: the skill is left as it is. Run '{later}' to make the change.\n")
        return False
    if not assume_yes:
        stdout.write(f"{question} (y/n) [n]: ")
        stdout.flush()
        if (stdin.readline() or "").strip().lower() not in ("y", "yes"):
            stdout.write(f"Left as it is. Run '{later}' to make the change later.\n")
            return False
    try:
        if want:
            footprint.write_skill(skill, claude_root)
        else:
            footprint.remove_skill(skill, claude_root)
    except OSError as exc:
        stdout.write(f"Could not change {path}: {exc}\n")
        return False
    when = (
        "when you finish a piece of work"
        if skill == capture_catalogue.FEEDBACK_SKILL
        else "followed by your request, before Claude starts on it"
    )
    stdout.write(
        f"Done. Run /{skill} in Claude Code {when} (start a new session if it isn't listed yet).\n"
        if want
        else "Removed.\n"
    )
    return True


def _capture_prune(
    config_dir: Path, *, retention_days: int | None, dry_run: bool, stdout, now: datetime
) -> int:
    """``capture prune``: delete capture signal files
    (:func:`~claudeglass.signals.prune`), old
    ``capture-log.jsonl`` records (:func:`~claudeglass.config.
    prune_capture_log`) and old ``usage-log.csv`` rows (SIG-5:
    :func:`~claudeglass.tools.log_usage.prune_usage_log`) older
    than ``retention_days`` (or :data:`SIGNAL_RETENTION_DEFAULT_DAYS`
    when unset) -- the same telemetry housekeeping ``serve``'s watcher
    already does on every tick (SEC-P8/G7), offered here for someone who
    isn't running the service, or wants to run it once by hand or on
    their own schedule."""
    days = retention_days or SIGNAL_RETENTION_DEFAULT_DAYS
    if dry_run:
        stdout.write(
            f"Dry run: nothing pruned. Run 'claudeglass capture prune' to delete signal files, "
            f"capture-log.jsonl records and usage-log.csv rows older than {days} days.\n"
        )
        return 0
    signals_removed = signals_mod.prune(config_dir, days, now=now)
    log_removed = prune_capture_log(config_dir, days, now=now)
    usage_log_path = log_usage_mod.default_usage_log_path(config_dir)
    usage_rows_removed = log_usage_mod.prune_usage_log(usage_log_path, days, now=now)
    stdout.write(
        f"Pruned {signals_removed} signal file(s), {log_removed} capture-log record(s) and "
        f"{usage_rows_removed} usage-log row(s) older than {days} days.\n"
    )
    return 0


def _capture_status(
    capture: CaptureConfig, *, config_dir: Path, claude_root: Path, stdout, args=None, config: Config | None = None
) -> int:
    stdout.write(f"Metrics capture: {capture_view.describe(capture)}\n")
    ids = capture.active_metrics()
    if capture.is_on:
        if capture.expired():
            stdout.write("Its end time has passed, so the hook adds nothing now. Turn it back on with 'claudeglass capture on'.\n")
        if capture.projects:
            only = [p for p in capture.projects if not p.startswith("!")]
            skip = [p[1:] for p in capture.projects if p.startswith("!")]
            stdout.write(
                "Projects: " + "; ".join(
                    part for part in (
                        f"only those matching {', '.join(only)}" if only else "",
                        f"not those matching {', '.join(skip)}" if skip else "",
                    ) if part
                ) + "\n"
            )
    by_group: dict[str, list[str]] = {}
    for metric_id in ids:
        by_group.setdefault(capture_catalogue.METRICS_BY_ID[metric_id].group, []).append(metric_id)
    for group, members in by_group.items():
        stdout.write(f"  {group}: {', '.join(members)}\n")
    if capture.coaching:
        stdout.write(f"  coaching: {', '.join(capture.coaching)}\n")
    cost = _capture_cost_lines(ids)
    if cost:
        stdout.write("Rough size:\n")
        for line in cost:
            stdout.write(f"  - {line}\n")
    elif capture.is_on:
        stdout.write("It adds nothing to Claude's context at this level.\n")
    if args is not None and config is not None:
        for line in _capture_measured(capture, args=args, config=config, config_dir=config_dir):
            stdout.write(f"{line}\n")
        if "big_output" in ids or "web" in ids:
            # CAP-9/F10: a real measured figure, replacing the old
            # unsourced "a fraction of a second" guess.
            deep_wait = _measure_deep_wait(args, config, config_dir).summary()
            if deep_wait:
                stdout.write(f"{deep_wait}\n")
        # SURV-HE: a hook failing on most calls is a prompt here, never
        # an automatic edit -- settings.json is only ever changed after
        # a shown diff and a yes, elsewhere in this command.
        recommendation = _scan_hook_errors(args, config, config_dir).recommendation()
        if recommendation:
            stdout.write(f"{recommendation}\n")
    wanted = hook_health.capture_specs(capture.hook_metrics())
    health = hook_health.check_capture(wanted, claude_root=claude_root, config_dir=config_dir, check_python=True)
    if wanted or health.extra:
        stdout.write(f"Hooks: {health.summary()}\n")
    if capture.coaching_notes_on:
        from . import coaching

        for line in coaching.describe(coaching.read(config_dir)):
            stdout.write(f"{line}\n")
        if args is not None and config is not None:
            for line in _coaching_measured(args, config, config_dir):
                stdout.write(f"{line}\n")
    if "feedback_skill" in capture.feedback:
        from . import footprint

        note = capture_view.SKILL_NOTES.get(footprint.feedback_skill_state(claude_root))
        if note:
            stdout.write(f"{note}\n")
    if "brief_templates" in capture.coaching:
        from . import footprint

        note = capture_view.BRIEF_SKILL_NOTES.get(footprint.skill_state(capture_catalogue.BRIEF_SKILL, claude_root))
        if note:
            stdout.write(f"{note}\n")
    lines_on = [m for m in ("feedback_note", "coaching_line") if m in capture.feedback or m in capture.coaching]
    if lines_on:
        from . import footprint

        try:
            settings = json.loads(hook_health.settings_path(claude_root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = None
        if not footprint.is_own_statusline(settings if isinstance(settings, dict) else None):
            stdout.write(f"{capture_view.STATUSLINE_NOTES[lines_on[-1]]}\n")
    stdout.write(
        "\nChange it: claudeglass capture level " + "|".join(capture_catalogue.LEVELS)
        + ", capture enable|disable METRIC..., or " + pages.plain("{{page:setup/capture}}") + " on the dashboard.\n"
    )
    return 0


def _capture_measured(capture: CaptureConfig, *, args, config: Config, config_dir: Path) -> list[str]:
    """While capture is on, what it has cost since it was turned on;
    while it is off, what each level would have cost you."""
    from . import capture as capture_mod
    from .report import _report_units

    if capture.is_on and not capture.enabled_at:
        return []
    rates = _capture_pricing(args, config, config_dir)
    if rates is None:
        return []
    if capture.is_on:
        corpus = _capture_corpus(args, config, config_dir, since=capture.enabled_at)
        units = _report_units(corpus, rates, config, config_dir)
        return _capture_usage_lines(capture_mod.usage(corpus, rates, since=capture.enabled_at), units)
    past, units = _capture_history(args, config, config_dir)
    return _capture_estimate_lines(past, units) if past is not None else []


def _cmd_capture(args: argparse.Namespace, *, stdin=None, stdout=None, now: datetime | None = None) -> int:
    """``capture``: see and change metrics capture.

    ``status`` shows the level, the metrics on, their rough size and
    whether settings.json runs the hooks they need. ``on``, ``level``,
    ``enable`` and ``disable`` change ``[capture]`` in this tool's
    config.toml; a change that makes Claude use more tokens is shown
    with its rough size and made only after a yes (or ``--yes``). Then,
    when settings.json doesn't run exactly the hook entries the metrics
    need, the diff is shown and made after a yes (``connect`` does only
    this step). ``off`` switches capture off and leaves the entries,
    which add nothing while it is off; ``remove`` switches it off and
    takes them out. ``feedback on|off`` turns the ``/tl-feedback`` skill
    and its reminders on or off and adds or removes the skill file, after
    showing it and asking; enabling or disabling ``feedback_skill`` does
    the same. ``brief on|off`` does that for the ``/tl-brief`` skill (the
    ``brief_templates`` toggle). ``prune`` deletes signal files,
    ``capture-log.jsonl`` records and ``usage-log.csv`` rows older than
    ``retention_days`` (or
    :data:`~claudeglass.config.SIGNAL_RETENTION_DEFAULT_DAYS` when
    unset) -- the same housekeeping ``serve``'s watcher already does on
    every tick (SEC-P8/G7), offered here for someone not running the
    service. ``refresh`` works out the coaching notes' split points
    (``coaching.json``) now, as the service does daily. Coaching notes
    (``coaching_notes``) run through the same hook at any level: ``off``
    leaves them on, ``remove`` turns them off too. ``--dry-run`` changes
    nothing."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    now = now or datetime.now(timezone.utc)
    config_dir = _resolve_config_dir(args.config_dir)
    claude_root = _resolve_claude_root(getattr(args, "claude_root", None))
    try:
        config = load_config(config_dir=config_dir)
    except ConfigError as exc:
        stdout.write(f"config.toml has a problem, so capture can't be changed: {exc}\n")
        return 2
    current = config.capture
    action = args.action
    if action == "status":
        return _capture_status(
            current, config_dir=config_dir, claude_root=claude_root, stdout=stdout, args=args, config=config
        )
    if action == "prune":
        return _capture_prune(config_dir, retention_days=config.retention_days, dry_run=args.dry_run, stdout=stdout, now=now)
    if action == "refresh":
        return _capture_refresh(args, config, config_dir, stdout=stdout, now=now)

    changes: dict = {}
    try:
        if action == "on":
            changes["level"] = args.level or (current.level if current.is_on and current.level != "custom" else "essentials")
            if current.level == "custom" and not args.level:
                changes = {"metrics": list(current.metrics)}
        elif action == "level":
            if len(args.values) != 1 or args.values[0] not in capture_catalogue.LEVELS:
                raise ValueError(f"'capture level' needs one of: {', '.join(capture_catalogue.LEVELS)}")
            changes["level"] = args.values[0]
        elif action in ("enable", "disable"):
            changes = _capture_metric_changes(action, args.values, current)
        elif action in ("off", "remove"):
            changes["level"] = "off"
            if action == "remove" and current.coaching_notes_on:
                # Coaching notes run through the hook entries remove takes out.
                changes["coaching"] = [i for i in current.coaching if i != "coaching_notes"]
        elif action in _SKILL_SWITCHES:
            if len(args.values) != 1 or args.values[0] not in ("on", "off"):
                raise ValueError(f"'capture {action}' needs on or off")
            on = args.values[0] == "on"
            if action == "feedback":
                changes["feedback"] = feedback_ids(current.feedback, on)
            elif on:
                changes["coaching"] = list(current.coaching) + [
                    i for i in ("brief_templates",) if i not in current.coaching
                ]
            else:
                changes["coaching"] = [i for i in current.coaching if i != "brief_templates"]
        until = _capture_until(args, now)
        if until is not None and action not in ("off", "remove", *_SKILL_SWITCHES):
            changes["until"] = until
        if args.sample is not None and action not in ("off", "remove", *_SKILL_SWITCHES):
            changes["sample"] = args.sample
    except ValueError as exc:
        stdout.write(f"{exc}\n")
        return 2

    try:
        preview = set_capture(config_dir, now=now, dry_run=True, **changes) if changes else current
    except ConfigError as exc:
        stdout.write(f"{exc}\n")
        return 2
    original = current
    if action in _SKILL_SWITCHES:
        skill, title, what = _SKILL_SWITCHES[action]
        on = args.values[0] == "on"
        if preview != current:
            stdout.write(
                f"{title}: " + (f"{what} on" if on else "off")
                + (". Nothing is added to Claude's context until you run the skill.\n" if on else ".\n")
            )
            if args.dry_run:
                stdout.write("Dry run: config.toml left unchanged.\n")
            else:
                try:
                    set_capture(config_dir, now=now, **changes)
                except ConfigError as exc:
                    stdout.write(f"{exc}\n")
                    return 2
                stdout.write("Saved to config.toml.\n")
        else:
            stdout.write(f"{title} {'are' if action == 'brief' else 'is'} already {'on' if on else 'off'}.\n")
        _capture_skill_step(
            on,
            claude_root=claude_root,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            stdin=stdin,
            stdout=stdout,
            skill=skill,
        )
        return 0
    if preview != current:
        if capture_view.describe(current) != capture_view.describe(preview):
            stdout.write(f"Metrics capture: {capture_view.describe(current)} -> {capture_view.describe(preview)}\n")
        if any(i not in current.feedback for i in preview.feedback) and preview.level == "deep":
            stdout.write(
                "Deep also turns on the /tl-feedback survey, its reminder note, and Claude's one-line reminder "
                "to run it. 'claudeglass capture feedback off' turns them off.\n"
            )
        if action == "disable":
            also = [m for m in current.active_metrics() if m not in preview.active_metrics() and m not in args.values]
            if also:
                stdout.write(f"{', '.join(also)} need{'s' if len(also) == 1 else ''} {', '.join(args.values)}, so {'it goes' if len(also) == 1 else 'they go'} too.\n")
        added =[m for m in preview.active_metrics() if m not in current.active_metrics()]
        costly = [m for m in added if capture_catalogue.asks_claude(m)]
        coaching_added = preview.coaching_notes_on and not current.coaching_notes_on
        if coaching_added:
            stdout.write(f"\n{capture_view.COACHING_NOTES_ON}\n")
        if costly:
            stdout.write(
                "\nThis makes Claude use more of your tokens. It adds " + ", ".join(costly) + ", and Claude then "
                "reads a short note and ends its replies with a one-line tag such as [tl: task=bugfix brief=clear].\n"
                "At this setting, roughly:\n"
            )
            for line in _capture_cost_lines(preview.active_metrics()):
                stdout.write(f"  - {line}\n")
            if preview.sample < 100:
                stdout.write(f"  (in {preview.sample}% of sessions)\n")
            stdout.write(pages.plain("{{page:setup/capture}} and 'claudeglass capture status' show what it really costs once it runs.\n"))
        if args.dry_run:
            stdout.write("Dry run: config.toml left unchanged.\n")
        else:
            if (costly or coaching_added) and not args.yes:
                stdout.write("Go ahead? (y/n) [n]: ")
                stdout.flush()
                if (stdin.readline() or "").strip().lower() not in ("y", "yes"):
                    stdout.write("Left unchanged.\n")
                    return 1
            try:
                current = set_capture(config_dir, now=now, **changes)
            except ConfigError as exc:
                stdout.write(f"{exc}\n")
                return 2
            stdout.write("Saved to config.toml. It takes effect in new sessions and subagents.\n")
    elif action != "connect":
        stdout.write(f"Metrics capture is already {capture_view.describe(current)}.\n")

    skill_on = "feedback_skill" in preview.feedback
    brief_on = "brief_templates" in preview.coaching
    for name, now_on, was_on in (
        (capture_catalogue.FEEDBACK_SKILL, skill_on, "feedback_skill" in original.feedback),
        (capture_catalogue.BRIEF_SKILL, brief_on, "brief_templates" in original.coaching),
    ):
        # on/level change the skills only through a switch into Deep
        # (config.set_capture adds its feedback items).
        if (action in ("on", "level", "enable", "disable") and now_on != was_on) or (action == "connect" and now_on):
            _capture_skill_step(
                now_on,
                claude_root=claude_root,
                dry_run=args.dry_run,
                assume_yes=args.yes,
                stdin=stdin,
                stdout=stdout,
                skill=name,
            )
    if action == "off":
        if preview.coaching_notes_on:
            stdout.write(
                "Coaching notes stay on: they don't depend on the capture level. "
                "'claudeglass capture disable coaching_notes' turns them off.\n"
            )
        elif hook_health.check_capture((), claude_root=claude_root, config_dir=config_dir).extra:
            stdout.write(
                "The capture hooks stay in settings.json and add nothing while capture is off. "
                "'claudeglass capture remove' takes them out.\n"
            )
        return 0
    if action == "remove" and skill_on:
        stdout.write(
            "The /tl-feedback skill stays: it works with capture off. "
            "'claudeglass capture feedback off' removes it.\n"
        )
    if action == "remove" and brief_on:
        stdout.write(
            "The /tl-brief skill stays: it works with capture off. "
            "'claudeglass capture brief off' removes it.\n"
        )
    wanted = () if action == "remove" else hook_health.capture_specs(preview.hook_metrics())
    if action == "connect" and not preview.is_on and not preview.coaching_notes_on:
        stdout.write("Capture is off, so no hook entries are needed. 'claudeglass capture on' turns it on.\n")
        return 0
    done = _capture_settings_step(
        wanted,
        config_dir=config_dir,
        claude_root=claude_root,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        stdin=stdin,
        stdout=stdout,
        removing=action == "remove",
    )
    if not done and wanted and not args.dry_run:
        stdout.write("Until then the chosen metrics can't be captured.\n")
    return 0


def _ask(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    sys.stdout.write(question + " (y/n) [n]: ")
    sys.stdout.flush()
    return (sys.stdin.readline() or "").strip().lower() in ("y", "yes")


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """``uninstall``: take claudeglass back out, step by step.

    1. Remove its SessionStart hook and statusline from settings.json
       (the diff is shown, and the file backed up first).
    2. Remove the logon service, if registered.
    3. With ``--revert-changes``: undo every applied change still in
       place, newest first (``apply --revert``; a file edited since is
       skipped and reported, never overwritten).
    4. With ``--delete-data``: delete the data folder, including the
       backups, so it runs last. Refuses while applied changes are
       still in place unless they were just reverted, and refuses
       (ROB-P7) while settings.json still runs a claudeglass hook
       from that folder -- step 1 declined, or failed -- since deleting
       it then would leave Claude Code calling a hook script that no
       longer exists.

    The package itself is removed with pip (printed at the end)."""
    from . import footprint

    config_dir = _resolve_config_dir(args.config_dir)
    projects_root_path = _service_projects_roots(args)
    claude_root = _resolve_claude_root(args.claude_root)
    plan = footprint.plan_uninstall(config_dir, claude_root=claude_root)
    dry = args.dry_run
    problems = 0

    print("1. Claude Code settings")
    if plan.new_settings_text is None:
        print("   Nothing to remove: settings.json does not run this tool.\n")
    else:
        for line in plan.settings_changes:
            print(f"   - {line}")
        print("\n" + plan.settings_diff)
        if dry:
            print("   Dry run: settings.json left unchanged.\n")
        elif _ask("   Remove these entries? settings.json is backed up first.", assume_yes=args.yes):
            try:
                backup = footprint.remove_settings_entries(plan)
            except OSError as exc:
                problems += 1
                print(f"   Could not remove them: {exc}\n")
            else:
                print(f"   Removed. The previous settings.json is at {backup}")
                print(f"   {RESTART_NOTE}\n")
        else:
            print("   Left unchanged.\n")
    for name, skill_file in (
        (capture_catalogue.FEEDBACK_SKILL, plan.feedback_skill),
        (capture_catalogue.BRIEF_SKILL, plan.brief_skill),
    ):
        if skill_file is None:
            continue
        print(f"   The /{name} skill: {footprint.home_label(skill_file)}")
        if dry:
            print("   Dry run: the skill is left in place.\n")
        elif _ask("   Remove the skill?", assume_yes=args.yes):
            try:
                footprint.remove_skill(name, claude_root)
                print("   Removed.\n")
            except OSError as exc:
                problems += 1
                print(f"   Could not remove it: {exc}\n")
        else:
            print("   Left in place.\n")

    print("2. Dashboard at logon")
    registered = installer_mod.is_registered()
    if registered is False:
        print("   Not registered.\n")
    else:
        service_plan = installer_mod.plan_service_install(sys.executable, projects_root_path, config_dir)
        if dry or _ask("   Remove the logon registration?", assume_yes=args.yes):
            problems += installer_mod.uninstall(service_plan, dry_run=dry) != 0
        print()

    print("3. Changes applied to your Claude Code settings and agent files")
    if not plan.applied:
        print("   None in place.\n")
    elif not args.revert_changes:
        print("   Still in place (kept; add --revert-changes to undo them all):")
        for backup in plan.applied:
            print(f"   - {backup.ts} ({backup.profile_id}): claudeglass apply --revert {backup.ts}")
        print()
    else:
        from .profiles import apply as apply_mod

        for backup in plan.applied:
            if dry:
                print(f"   Would undo {backup.ts} ({backup.profile_id}).")
                continue
            try:
                result = apply_mod.revert(backup.ts, config_dir=config_dir)
            except apply_mod.ApplyError as exc:
                problems += 1
                print(f"   Could not undo {backup.ts}:")
                for reason in exc.reasons:
                    print(f"     {reason}")
                continue
            print(f"   Undid {backup.ts}: {len(result.restored)} restored, {len(result.deleted)} removed.")
        print()

    print("4. This tool's data folder")
    if plan.data_dir is None:
        print("   Nothing to delete.\n")
    elif not args.delete_data:
        print(f"   Kept: {footprint.home_label(plan.data_dir)} (add --delete-data to delete it).\n")
    else:
        still_applied = [b for b in footprint.plan_uninstall(config_dir, claude_root=claude_root).applied] if not dry else (
            [] if args.revert_changes else plan.applied
        )
        # ROB-P7: re-check live settings.json, not a flag tracked through
        # step 1 -- a "no" there, or a step 1 that failed to write, both
        # leave hook entries in place the same way.
        hooks_remain = not dry and (
            hook_health.check(config_dir, claude_root=claude_root).command is not None
            or hook_health.check_capture((), claude_root=claude_root).extra
        )
        if still_applied:
            problems += 1
            print(
                "   Not deleted: it holds the backups for changes still in place, and deleting them would leave "
                "you no way to undo those changes. Undo them first (--revert-changes).\n"
            )
        elif dry:
            print(f"   Would delete {footprint.home_label(plan.data_dir)}.\n")
        elif hooks_remain:
            problems += 1
            print(
                "   Not deleted: settings.json still runs claudeglass hooks from this folder (step 1 "
                "above). Deleting it now would leave Claude Code calling hook scripts that no longer exist, "
                "failing silently on every session or tool call. Remove the entries first (answer yes at step "
                "1, or run 'claudeglass capture remove'), then run 'claudeglass uninstall --delete-data' again.\n"
            )
        elif _ask(f"   Delete {footprint.home_label(plan.data_dir)}? This cannot be undone.", assume_yes=args.yes):
            failures = footprint.delete_data(plan.data_dir)
            if failures:
                problems += 1
                print("   Some files could not be deleted (stop a running dashboard first):")
                for failure in failures:
                    print(f"     {failure}")
            else:
                print("   Deleted.")
            print()

    pyz = installer_mod.detect_pyz_path()
    if pyz is not None:
        print(f"Finally, remove the program itself: delete {pyz}")
    else:
        python = invocation._python_word(invocation._terminal_python(sys.executable))
        print(f"Finally, remove the program itself with: {python} -m pip uninstall claudeglass")
    return 1 if problems else 0


def _cmd_baseline(args: argparse.Namespace) -> int:
    """``baseline``: capture a new baseline (default), or ``--list``/
    ``--show ID`` an already-saved one. Shares its project/window
    resolution with every report-like subcommand
    (``_resolve_project_dirs_for_args``/``_load_config_and_pricing``).
    """
    config_dir = _resolve_config_dir(args.config_dir)

    if args.list_baselines:
        records = baseline_mod.list_baselines(config_dir)
        if not records:
            print(f"claudeglass baseline: no baselines saved yet under {config_dir}", file=sys.stderr)
            return 1
        for record in records:
            flag = " (provisional)" if record.get("provisional") else ""
            print(f"{record['id']}  {record.get('created_at', '')}  sessions={record.get('sessions_analysed', 0)}{flag}")
        return 0

    if args.show:
        record = baseline_mod.load_baseline(config_dir, args.show)
        if record is None:
            print(f"claudeglass baseline: no baseline {args.show!r} under {config_dir}", file=sys.stderr)
            return 1
        print(baseline_mod.render_onboarding_report(record))
        return 0

    config, rates, config_dir, err = _load_config_and_pricing(args)
    if err is not None:
        return err

    root, project_dirs = _resolve_project_dirs_for_args(args, config)
    if not project_dirs:
        print(f"claudeglass baseline: no matching project directories under {root}", file=sys.stderr)
        return 1

    record, _model = baseline_mod.build_baseline(
        config=config,
        pricing=rates,
        config_dir=config_dir,
        project_dirs=project_dirs,
        days=args.days,
        finalise=args.finalise,
    )
    report_markdown = baseline_mod.render_onboarding_report(record)
    path = baseline_mod.save_baseline(config_dir, record, report_markdown)
    print(f"Wrote {path}")
    print(report_markdown)

    status = baseline_mod.capture_status(config)
    print(baseline_mod.format_capture_status(status))
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    """``backtest``: read-only. Prints every prediction the dashboard's
    ``POST /api/whatif`` has logged (``"log": true``), and its verdict
    once judged (EST-P4, :mod:`backtest`) -- from the store at
    ``<config-dir>/service.db`` without writing to it, the same
    read-only posture ``_merge_dashboard_marks`` already gives the
    report's own session tags/ratings. Judging itself (matching a
    prediction to the change point it turned into, and comparing the
    sessions before and after) happens where the data already lives: a
    running ``serve``'s watcher tick, or its own ``GET /api/backtest``
    call. A prediction still waiting on a match or more data is listed
    separately, not silently dropped.
    """
    from .service.serve import STORE_FILENAME
    from .service.store import read_predictions

    config_dir = _resolve_config_dir(args.config_dir)
    predictions = read_predictions(config_dir / STORE_FILENAME)
    if not predictions:
        print(
            f"claudeglass backtest: no predictions logged yet under {config_dir}. Log one from the "
            "dashboard's What if? with 'log' on, then let 'serve' run (or open the dashboard) to judge it.",
            file=sys.stderr,
        )
        return 1

    judged = [p for p in predictions if p["judged_at"]]
    pending = [p for p in predictions if not p["judged_at"]]

    def _usd(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "-"

    if judged:
        print(f"{'ts':20}  {'measure':22}  {'agent':12}  {'predicted $':>11}  {'measured $':>11}  verdict")
        for row in judged:
            print(
                f"{row['ts']:20}  {row['measure_key']:22}  {row['agent'] or '-':12}  "
                f"{_usd(row['predicted_usd']):>11}  {_usd(row['measured_usd']):>11}  {row['verdict']}"
            )
    if pending:
        if judged:
            print()
        print(f"{len(pending)} prediction{'s' if len(pending) != 1 else ''} still waiting on a match or more data.")
    return 0


# -- team aggregate import / team-report (v0.3 Task 1) -----------------------


def _cmd_import(args: argparse.Namespace) -> int:
    """``import FILE...``: validate every file first (schema/length
    allowlist -- :func:`team.validate_team_document`), then copy each
    into ``<config_dir>/team/<machine_id>-<generated_at>.json``. Exits 2
    on the first invalid file, naming it and the reason, per the plan's
    own "schema-checked ... exit 2 with the reason" contract; nothing is
    written for *any* file once one has failed, so a bad batch never
    partially imports.
    """
    from . import team as team_mod

    config_dir = _resolve_config_dir(args.config_dir)

    documents: list[tuple[str, dict]] = []
    for file_arg in args.files:
        path = Path(file_arg)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"claudeglass import: cannot read {file_arg}: {exc}", file=sys.stderr)
            return 2
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"claudeglass import: {file_arg} is not valid JSON: {exc}", file=sys.stderr)
            return 2
        reason = team_mod.validate_team_document(doc)
        if reason is not None:
            print(f"claudeglass import: {file_arg} rejected: {reason}", file=sys.stderr)
            return 2
        documents.append((file_arg, doc))

    for file_arg, doc in documents:
        try:
            saved_path = team_mod.save_team_document(config_dir, doc)
        except (OSError, ValueError) as exc:
            print(f"claudeglass import: cannot write {file_arg}: {exc}", file=sys.stderr)
            return 2
        print(f"Imported {file_arg} -> {saved_path}")
    return 0


def _cmd_team_report(args: argparse.Namespace) -> int:
    """``team-report``: the cross-machine comparison built from every
    document already imported into ``<config_dir>/team/`` (see
    ``import``). Reads no project/session data of its own at all --
    only the already-aggregated, already-privacy-checked documents on
    disk.
    """
    from . import team as team_mod

    config_dir = _resolve_config_dir(args.config_dir)
    documents = team_mod.load_latest_team_documents(config_dir)
    if not documents:
        print(
            f"claudeglass team-report: no team documents under {config_dir / 'team'} -- "
            "run `claudeglass import <file>...` first",
            file=sys.stderr,
        )
        return 1

    section = team_mod.build_team_report_section(documents, min_sessions=args.min_sessions)
    machine_ids = sorted({str(doc.get("machine_id", "?")) for doc in documents})

    meta = ReportMeta(
        tool_version=__version__,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=f"{len(documents)} machine(s): {', '.join(machine_ids)}",
    )
    model = ReportModel(meta=meta, sections=[section], recommendations=[], diagnostics=Diagnostics())
    _emit_report_outputs(model, args)
    return 0


# -- apply (v0.3 milestone) ---------------------------------------------------


def _load_profile_arg(profile_arg: str):
    """``(Profile, None)`` on success, or ``(None, message)`` on
    failure -- ``profile_arg`` is tried as a catalogue id first
    (:func:`~claudeglass.profiles.catalogue.get`), then as a path
    to a profile TOML file (:func:`~claudeglass.profiles.schema.load_profile`).
    """
    from .profiles import catalogue as catalogue_mod
    from .profiles.schema import ProfileError, load_profile

    catalogue_profile = catalogue_mod.get(profile_arg)
    if catalogue_profile is not None:
        return catalogue_profile, None

    path = Path(profile_arg)
    if not path.is_file():
        return None, f"no such catalogue profile or profile file: {profile_arg}"
    try:
        return load_profile(path), None
    except ProfileError as exc:
        return None, str(exc)


#: Profile id recorded in the backup manifest for an ``apply --set``.
ONE_OFF_PROFILE_ID = "one-off"


def _parse_set_value(raw: str, spec) -> object:
    """``--set``'s VALUE as the allowlisted key's type: true/false for a
    bool, a number for an int, a comma-separated list for a list (empty
    means an empty list), NAME:VALUE pairs for a map, else the text
    itself."""
    if spec is None:
        return raw
    if spec.kind == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
        return raw
    if spec.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if spec.kind == "list[str]":
        return [item.strip() for item in raw.split(",") if item.strip()]
    if spec.kind.startswith("map["):
        # NAME:VALUE pairs, comma-separated: skillOverrides=pdf:off,xlsx:name-only
        out: dict = {}
        for pair in raw.split(","):
            name, sep, value = pair.strip().rpartition(":")
            if not sep or not name:
                return raw
            value = value.strip()
            if spec.kind == "map[str,bool]":
                lowered = value.lower()
                if lowered not in ("true", "false"):
                    return raw
                out[name.strip()] = lowered == "true"
            else:
                out[name.strip()] = value
        return out
    return raw


def _one_off_profile(set_values: list[str], agent: str | None):
    """An ad-hoc profile holding just the ``--set`` changes, validated
    through the same allowlist and range checks as a real profile.
    Returns ``(profile, None)`` or ``(None, error text)``.

    COV-07/COV-11: a ``--set env.NAME=value`` key routes into the
    profile's ``env`` table instead of ``settings``/``agents`` -- an env
    value is always the free-form string the user typed (the allowlist
    only governs which *names* are allowed, per ``schema.LeverSpec``'s
    own docstring), so it skips ``_parse_set_value``'s bool/int/list
    coercion. A command mixes ``env.NAME=value`` with a settings/agent
    ``--set`` no more than a real profile mixes them across tables in one
    ``--set`` invocation would be ambiguous about scope."""
    from .profiles import schema

    env_items = [item for item in set_values if item.partition("=")[0].strip().startswith("env.")]
    if env_items and len(env_items) != len(set_values):
        return None, "--set: env.NAME=value can't be mixed with a settings/agent --set in the same command"
    if env_items and agent:
        return None, "--set env.NAME=value: --agent doesn't apply to an environment variable"

    allowlist = schema.AGENT_ALLOWLIST if agent else schema.SETTINGS_ALLOWLIST
    values: dict = {}
    for item in set_values:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            return None, f"--set {item!r}: expected KEY=VALUE"
        if env_items:
            values[key[len("env.") :]] = raw
        else:
            values[key] = _parse_set_value(raw, allowlist.get(key))
    doc: dict = {"id": ONE_OFF_PROFILE_ID, "name": "One-off change"}
    if env_items:
        doc["env"] = values
    elif agent:
        doc["agents"] = {agent: values}
    else:
        doc["settings"] = values
    try:
        return schema.load_dict(doc), None
    except schema.ProfileError as exc:
        return None, str(exc)


def _cmd_apply(args: argparse.Namespace) -> int:
    """``apply``: apply a profile's allowlisted settings/agent/env
    levers to a project or the current user (plan Milestone v0.3's
    ``apply`` bullet -- see ``profiles/apply.py``'s module docstring for
    the full resolution/backup/revert contract this delegates to).

    Exit codes: 0 success (``--list-backups``, a successful ``--revert``,
    or a ``--dry-run`` whose plan is not blocked), 1 a refused *real*
    apply (a git-tracked target without ``--allow-tracked``, a missing
    agent file without ``--force``, or an existing file this command
    cannot parse), 2 bad input (an unrecognised profile, a scope/
    ``--project`` mismatch, an unknown ``--revert`` timestamp) -- fix
    S1: also a ``--dry-run`` whose plan *would* be refused, so the dry
    run a user runs specifically to find out whether an apply will work
    doesn't print a clean diff and exit 0 for one that wouldn't.
    """
    from .profiles import apply as apply_mod

    command = "apply"
    config_dir = _resolve_config_dir(args.config_dir)
    claude_root = _resolve_claude_root(args.claude_root)

    if args.list_backups:
        backups = apply_mod.list_backups(config_dir)
        if not backups:
            print("No backups found.")
            return 0
        for backup in backups:
            print(f"{backup.ts}  profile={backup.profile_id}  scope={backup.scope}  files={backup.file_count}")
        return 0

    if args.revert:
        try:
            result = apply_mod.revert(
                args.revert, config_dir=config_dir, ignore_changes=getattr(args, "ignore_changes", False)
            )
        except apply_mod.ApplyError as exc:
            print(f"claudeglass {command}: {exc}", file=sys.stderr)
            return 2
        print(
            f"Reverted {args.revert}: restored {len(result.restored)} file(s), "
            f"removed {len(result.deleted)} file(s)."
        )
        print(RESTART_NOTE)
        return 0

    if args.set_values and args.profile:
        print(f"claudeglass {command}: give a profile or --set, not both", file=sys.stderr)
        return 2
    if args.agent and not args.set_values:
        print(f"claudeglass {command}: --agent only goes with --set", file=sys.stderr)
        return 2
    if not args.profile and not args.set_values:
        print(
            f"claudeglass {command}: a profile id or path is required "
            "(or use --set/--revert/--list-backups)",
            file=sys.stderr,
        )
        return 2

    if args.set_values:
        profile, err = _one_off_profile(args.set_values, args.agent)
    else:
        profile, err = _load_profile_arg(args.profile)
    if err is not None:
        print(f"claudeglass {command}: {err}", file=sys.stderr)
        return 2

    project_path = Path(args.project_dir) if args.project_dir else None
    scope = args.scope or ("project-local" if project_path else "user")
    if scope != "user" and project_path is None:
        print(f"claudeglass {command}: --scope {scope} requires --project-dir", file=sys.stderr)
        return 2

    snaps = snapshots.load_snapshots(config_dir)
    # COV-04: an apply that targets a project should warn/plan against
    # *that project's* own snapshot, not whichever project happened to
    # snapshot most recently (the old `snaps[-1]` picked up any project's
    # newest snapshot, so a provenance/override warning could compare a
    # project's settings against a different project's snapshot entirely).
    # A user-scope apply with no --project-dir keeps the old newest-overall
    # behaviour, since there is no single project to key on.
    if project_path is not None:
        project_key = snapshots.snapshot_project_key(discovery.slug_for(str(project_path)))
        latest_snapshot = snapshots.latest_snapshot_per_project(snaps).get(project_key) if snaps else None
    else:
        latest_snapshot = snaps[-1] if snaps else None

    if args.launch:
        managed = set(snapshots.managed_keys(latest_snapshot)) if latest_snapshot else set()
        path = apply_mod.write_launch_overlay(profile, config_dir=config_dir, managed_keys=managed)
        print(f"Wrote {path}")
        # PROF-02: a launch overlay is session-only (the file backs a
        # single `claude` invocation's --settings flag, never written into
        # any persisted settings file), so an effort level the profile
        # carries has to ride along on the same command line -- otherwise
        # the printed command would launch Claude Code without it, silently
        # dropping the one setting a launch overlay exists to convey.
        effort = profile.settings.get("effortLevel")
        launch_command = f"claude --settings {path}"
        if isinstance(effort, str) and effort:
            launch_command += f" --effort {effort}"
        print(launch_command)
        print("Session-only: applies to this one `claude` invocation, not written to any settings file.")
        return 0

    try:
        plan = apply_mod.plan_apply(
            profile,
            scope=scope,
            project_path=project_path,
            config_dir=config_dir,
            claude_root=claude_root,
            snapshot=latest_snapshot,
            allow_tracked=args.allow_tracked,
            force=args.force,
            mark_active=not args.set_values,
        )
    except ValueError as exc:
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return 2
    except apply_mod.ApplyError as exc:
        print(f"claudeglass {command}: {exc}", file=sys.stderr)
        return 1

    explanation = apply_mod.explain_plan(plan)
    if args.dry_run:
        from .profiles.diff import apply_command

        for line in explanation:
            print(line)
        if explanation:
            print()
        print(plan.diff_text if plan.diff_text else "No changes to apply.")
        if plan.skipped_managed:
            for key in plan.skipped_managed:
                print(f"# {key}: managed by policy, raise with your administrator")
        if plan.overridden:
            # COV-04: writing these keys wouldn't change what Claude Code
            # actually uses -- a higher-precedence layer already supplies
            # the value.
            print("Already overridden by a higher-precedence layer (writing won't change what Claude Code uses):")
            for note in plan.overridden:
                print(f"  # {note}")
        if plan.blocked:
            # Fix S1: a real apply of this plan would refuse -- say so
            # here too, rather than printing a clean diff and exiting 0
            # as if the apply would succeed.
            for reason in plan.blocked:
                print(f"claudeglass {command}: would be refused: {reason}", file=sys.stderr)
            return 2
        if args.set_values:
            print("To make this change, run the same command without --dry-run.")
        else:
            suggested = apply_command(
                plan.profile_id, scope, str(project_path) if project_path else None
            )
            print(suggested)
        return 0

    if plan.blocked:
        for reason in plan.blocked:
            print(f"claudeglass {command}: refused: {reason}", file=sys.stderr)
        return 1

    for line in explanation:
        print(line)
    if plan.overridden:
        print("Already overridden by a higher-precedence layer (writing won't change what Claude Code uses):")
        for note in plan.overridden:
            print(f"  # {note}")
    result = apply_mod.execute(plan, config_dir=config_dir)
    print(f"Applied {plan.profile_id} ({scope}).")
    for path in result.written:
        print(f"  wrote {path}")
    print(f"To revert: claudeglass apply --revert {result.ts}")
    print(RESTART_NOTE)
    return 0


def _cmd_serve_purge(config_dir: Path, *, confirmed: bool, store_path: Path | None = None) -> int:
    """``serve --purge`` (S1-integration fix 2.e): delete
    ``<config-dir>/service.db`` (or ``--store``'s file) and its WAL/SHM
    sidecars. The store is
    always a derived cache (never source of truth -- see
    ``service/store.py``'s module docstring), so this is safe: the next
    ``serve`` run simply rebuilds it from the transcripts on disk.
    Always prints exactly what it would delete; only actually deletes
    when ``confirmed`` (``--yes``) is set. Refuses while a running
    ``serve`` holds the store's lock (``service/storelock.py``): deleting
    a database out from under it leaves that dashboard writing to a file
    nothing can see.
    """
    from .service import storelock
    from .service.serve import STORE_FILENAME

    db_path = store_path if store_path is not None else config_dir / STORE_FILENAME
    holder = storelock.holder(db_path)
    if holder is not None:
        from .service.serve import locked_message

        print(locked_message(db_path, holder, command="serve --purge"), file=sys.stderr)
        return 1
    candidates = [db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]
    existing = [p for p in candidates if p.exists()]

    if not existing:
        print(f"claudeglass serve --purge: nothing to delete ({db_path} does not exist)")
        return 0

    print("claudeglass serve --purge: will delete:")
    for path in existing:
        print(f"  {path}")

    if not confirmed:
        print("Re-run with --yes to actually delete these files.", file=sys.stderr)
        return 2

    # Review finding 15: don't let one un-removable sidecar (e.g. a WAL
    # file still open in another process, or a permissions problem) abort
    # the whole purge with an unhandled OSError -- delete what can be
    # deleted and report the rest, the same "always tell you exactly what
    # happened" posture as the rest of this command.
    deleted = 0
    failures: list[str] = []
    for path in existing:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    if failures:
        print(f"Deleted {deleted} file(s); {len(failures)} failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Deleted {deleted} file(s).")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Build ``ServeOptions`` from argv and hand off to
    ``service.serve.run`` (S1-api). Both ``service.serve`` and
    ``service.contracts`` are imported here rather than at module load
    time only to keep this module's own top-level import list free of
    the ``service`` package for readers who never touch ``serve`` --
    neither import risks the sibling S1-watcher package: ``serve.py``
    itself only reaches into ``service.watcher``/``service.rebuild``
    lazily, inside the functions that need them (see its module
    docstring).

    ``--billing-mode`` (S1-integration fix 1.a) defaults to
    ``config.toml``'s own ``billing`` setting (itself defaulting to
    ``"api"``) when not given on the command line, so a subscription
    user only has to say so once, in one place. ``--retention-days``
    falls back to ``config.toml``'s ``retention_days`` the same way.
    """
    config_dir = _resolve_config_dir(args.config_dir)
    store_path = Path(args.store_path) if args.store_path else None

    if args.purge:
        return _cmd_serve_purge(config_dir, confirmed=args.yes, store_path=store_path)

    from .service.contracts import ServeOptions
    from .service.serve import run as run_serve

    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"claudeglass serve: {exc}", file=sys.stderr)
        return 2
    projects_root, *extra_projects_roots = discovery.projects_roots(args.projects_root, config.extra_projects_roots)
    billing_mode = args.billing_mode if args.billing_mode is not None else config.billing
    # SEC-P5: config.toml's own retention_days was already bounds-checked
    # by load_config/_build_config; --retention-days is a raw CLI int
    # that bypasses that, and a bad value here is destructive (it feeds
    # Store.retention_prune/signals.prune, which delete data) rather than
    # just cosmetic, so it gets the same 1-36500 bound here.
    if args.retention_days is not None and not (RETENTION_DAYS_MIN <= args.retention_days <= RETENTION_DAYS_MAX):
        print(
            f"claudeglass serve: --retention-days must be between {RETENTION_DAYS_MIN} and "
            f"{RETENTION_DAYS_MAX}, got {args.retention_days}",
            file=sys.stderr,
        )
        return 2
    retention_days = args.retention_days if args.retention_days is not None else config.retention_days
    monthly_report_dir = Path(args.monthly_report_dir) if args.monthly_report_dir else None
    options = ServeOptions(
        projects_root=projects_root,
        extra_projects_roots=tuple(extra_projects_roots),
        config_dir=config_dir,
        port=args.port,
        bind=args.bind,
        poll_interval_s=args.poll_interval,
        retention_days=retention_days,
        exclude_projects=tuple(args.exclude_project or ()),
        billing_mode=billing_mode,
        monthly_report_dir=monthly_report_dir,
        allowed_hosts=tuple(args.allowed_host or ()),
        store_path=store_path,
        exit_on_code_change=args.exit_on_code_change,
    )
    try:
        return run_serve(options, once=args.once, allow_remote=args.allow_remote)
    except KeyboardInterrupt:
        return 0


def _insert_default_subcommand(argv: list[str]) -> list[str]:
    """If the first token isn't a known subcommand (and isn't a top-level
    flag argparse must see first, like --version), insert the default
    subcommand ahead of it.
    """
    if not argv:
        return [DEFAULT_SUBCOMMAND]
    first = argv[0]
    if first in SUBCOMMANDS or first in _LEADING_PASSTHROUGH:
        return argv
    return [DEFAULT_SUBCOMMAND, *argv]


#: Subcommands whose output is data for another program (Claude Code's
#: statusline, a settings.json fragment, an export), printed as written.
_DATA_OUTPUT = frozenset({"statusline", "snapshot-config", "export", "scrub-fixture"})


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    raw_argv = _insert_default_subcommand(raw_argv)

    # Every command this prints (help, notes, fixes, next steps) is
    # written as 'claudeglass ...', which runs only when pip's
    # Scripts folder is on PATH: print it in the form that runs this
    # install (invocation.py). JSON is swapped string by string where it
    # is rendered instead (a path in the prefix would break it as text).
    streams = sys.stdout, sys.stderr
    data = bool(raw_argv) and raw_argv[0] in _DATA_OUTPUT
    prefix = invocation.SHORT if data else invocation.command_prefix()
    # Under pythonw (the logon service) both are None and print() writes
    # nothing: a wrapper there would fail on the first print instead.
    if prefix != invocation.SHORT:
        if sys.stdout is not None:
            sys.stdout = invocation.RewritingStream(sys.stdout, prefix)
        if sys.stderr is not None:
            sys.stderr = invocation.RewritingStream(sys.stderr, prefix)
    try:
        parser = _make_parser()
        args = parser.parse_args(raw_argv)  # may raise SystemExit (--version, --help, errors)
        if getattr(args, "json", False):
            sys.stdout = streams[0]
        return _run(args)
    finally:
        sys.stdout, sys.stderr = streams


def _run(args: argparse.Namespace) -> int:
    command = args.command or DEFAULT_SUBCOMMAND

    if command == "pricing-check":
        return _cmd_pricing_check(args)
    if command == "snapshot-config":
        return _cmd_snapshot_config(args)
    if command == "probe-config":
        return _cmd_probe_config(args)
    if command == "report":
        return _cmd_report_like(args, include=None)
    if command in _REPORT_LIKE_SECTIONS:
        return _cmd_report_like(args, include={"overview", _REPORT_LIKE_SECTIONS[command]})
    if command == "config-diff":
        return _cmd_config_diff(args)
    if command == "log-usage":
        return _cmd_log_usage(args)
    if command == "probe":
        return _cmd_probe(args)
    if command == "statusline":
        return _cmd_statusline(args)
    if command == "export":
        return _cmd_export(args)
    if command == "monthly-report":
        return _cmd_monthly_report(args)
    if command == "compare":
        return _cmd_compare(args)
    if command == "reconcile":
        return _cmd_reconcile(args)
    if command == "scrub-fixture":
        return _cmd_scrub_fixture(args)
    if command == "apply":
        return _cmd_apply(args)
    if command == "init":
        return _cmd_init(args)
    if command == "baseline":
        return _cmd_baseline(args)
    if command == "backtest":
        return _cmd_backtest(args)
    if command == "serve":
        return _cmd_serve(args)
    if command == "install-service":
        return _cmd_install_service(args)
    if command == "uninstall-service":
        return _cmd_uninstall_service(args)
    if command == "update":
        return _cmd_update_finish(args) if args.finish else _cmd_update(args)
    if command == "import":
        return _cmd_import(args)
    if command == "team-report":
        return _cmd_team_report(args)
    if command == "changes":
        return _cmd_changes(args)
    if command == "status":
        return _cmd_status(args)
    if command == "review":
        return _cmd_review(args)
    if command == "check":
        return _cmd_check(args)
    if command == "uninstall":
        return _cmd_uninstall(args)
    if command == "capture":
        return _cmd_capture(args)

    print(f"claudeglass {command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

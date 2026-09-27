"""Tests for WP6's statusline renderer (src/claudeglass/statusline.py)."""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import installer, statusline


def _write_transcript(path: Path, assistant_ts_iso: str) -> None:
    lines = [
        {"type": "user", "timestamp": "2026-09-18T11:00:00.000Z", "message": {"content": "hi"}},
        {"type": "assistant", "timestamp": assistant_ts_iso, "message": {"content": []}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d))
            fh.write("\n")


# -- render_status: full and minimal payloads --------------------------------


def test_render_status_full_payload_matches_plan_example(tmp_path):
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    # 4m12s = 252s remaining.
    expires_at = now.timestamp() + 252

    payload = {
        "context_window": {"used_tokens": 143000},
        "prompt_cache": {"warm": True, "ttl": "5m", "expires_at": expires_at},
        "rate_limits": {
            "five_hour": {"used_percentage": 37},
            "seven_day": {"used_percentage": 12},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert line == "ctx 143k | cache warm 5m 04:12 | 5h 37% | 7d 12%"


def test_render_status_minimal_payload_falls_back():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({}, now, 300) == "claudeglass"
    assert statusline.render_status(None, now, 300) == "claudeglass"


def test_render_status_partial_payload_only_renders_present_segments():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"used_tokens": 50000}}
    assert statusline.render_status(payload, now, 300) == "ctx 50k"


def test_render_status_ctx_rounds_to_nearest_k():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"used_tokens": 143499}}
    assert statusline.render_status(payload, now, 300) == "ctx 143k"


def test_render_status_ctx_tolerates_missing_used_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({"context_window": {}}, now, 300) == "claudeglass"
    assert statusline.render_status({"context_window": "not a dict"}, now, 300) == "claudeglass"


def test_render_status_cache_warm_without_expires_at_omits_countdown():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m"}}
    assert statusline.render_status(payload, now, 300) == "cache warm 5m"


def test_render_status_cache_warm_1h_countdown():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "1h", "expires_at": now.timestamp() + 3570}}
    assert statusline.render_status(payload, now, 300) == "cache warm 1h 59:30"


def test_render_status_cache_cold_with_recache_hint():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False, "recache_tokens_if_cold": 12345}}
    assert statusline.render_status(payload, now, 300) == "cache cold recache ~12k tokens"


def test_render_status_cache_cold_without_recache_hint():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False}}
    assert statusline.render_status(payload, now, 300) == "cache cold"


def test_render_status_rate_limits_tolerate_missing_window():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 10}}}
    assert statusline.render_status(payload, now, 300) == "5h 10%"


# -- v3-limits: near-cap "!" warning marker ----------------------------------


def test_render_status_rate_segment_gets_warning_marker_at_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 92}}}
    assert statusline.render_status(payload, now, 300) == "5h 92%!"


def test_render_status_rate_segment_no_warning_marker_below_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"five_hour": {"used_percentage": 89}}}
    assert statusline.render_status(payload, now, 300) == "5h 89%"


def test_render_status_rate_segment_warning_marker_at_exactly_90_pct():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"rate_limits": {"seven_day": {"used_percentage": 90}}}
    assert statusline.render_status(payload, now, 300) == "7d 90%!"


def test_render_status_both_rate_segments_can_carry_warning_markers():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 100},
            "seven_day": {"used_percentage": 95},
        }
    }
    assert statusline.render_status(payload, now, 300) == "5h 100%! | 7d 95%!"


def test_render_status_line_length_stays_within_bound_with_warning_markers():
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {"used_tokens": 143000},
        "prompt_cache": {"warm": True, "ttl": "5m", "expires_at": now.timestamp() + 252},
        "rate_limits": {
            "five_hour": {"used_percentage": 100},
            "seven_day": {"used_percentage": 95},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    assert line.endswith("5h 100%! | 7d 95%!")


def test_render_status_effective_ttl_none_skips_ttl_segment(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, now.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    assert statusline.render_status(payload, now, None) == "claudeglass"


# -- TTL countdown from a constructed tmp transcript -------------------------


def test_render_status_ttl_expired_when_past_ttl(tmp_path):
    now = datetime(2026, 9, 18, 12, 10, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=600)  # 10 minutes ago, default estimate TTL is 5m
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, last_ts.isoformat().replace("+00:00", "Z"))
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m expired"


def test_render_status_ttl_1h_label_from_transcript_ephemeral_hint(tmp_path):
    """No ``prompt_cache`` on the payload falls back to the estimate,
    whose TTL is read from the transcript's own last assistant line
    (``message.usage.cache_creation.ephemeral_1h_input_tokens > 0``
    implies 1h) rather than from ``effective_ttl_s`` (kept only as an
    on/off gate -- see statusline.py's module docstring)."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=30)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": last_ts.isoformat().replace("+00:00", "Z"),
            "message": {"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 500, "ephemeral_5m_input_tokens": 0}}},
        }))
        fh.write("\n")
    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 1h 59:30"


def test_render_status_ttl_missing_transcript_path_skips_segment():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert statusline.render_status({}, now, 300) == "claudeglass"


def test_render_status_ttl_nonexistent_transcript_file_skips_segment(tmp_path):
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"transcript_path": str(tmp_path / "does-not-exist.jsonl")}
    assert statusline.render_status(payload, now, 300) == "claudeglass"


def test_render_status_ttl_reads_only_tail_of_large_transcript(tmp_path):
    """The countdown must come from the *last* assistant line even when
    the transcript is larger than the 64KB tail window (WP6 brief: "never
    load the whole file")."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    last_ts = now - timedelta(seconds=10)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        # Pad with plenty of filler lines so the file exceeds 64KB well
        # before the final, real assistant line.
        filler_content = "x" * 500
        for i in range(300):
            fh.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-09-18T00:00:00.000Z",
                "message": {"content": filler_content, "seq": i},
            }))
            fh.write("\n")
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": last_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": []},
        }))
        fh.write("\n")
    assert transcript.stat().st_size > statusline._TAIL_BYTES

    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m 04:50"


def test_render_status_ttl_survives_unicode_line_separator_inside_a_json_string(tmp_path):
    """fix(statusline): a U+2028/U+2029 (or bare \\r) embedded in a
    message string is legal JSON but is treated as a line break by
    ``str.splitlines()`` -- that would shear the final assistant line's
    JSON into two unparsable fragments and skip it, wrongly falling back
    to an older timestamp (or none at all). Scanning with
    ``str.split("\\n")`` instead must find the correct, newest timestamp.
    """
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = now - timedelta(seconds=200)
    new_ts = now - timedelta(seconds=10)
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": old_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": "no separators here"},
        }))
        fh.write("\n")
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": new_ts.isoformat().replace("+00:00", "Z"),
            "message": {"content": "before after"},
        }))
        fh.write("\n")

    payload = {"transcript_path": str(transcript)}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache est 5m 04:50"  # from new_ts, not old_ts


# -- resolve_effective_ttl ----------------------------------------------


def test_resolve_effective_ttl_default_is_300s():
    assert statusline.resolve_effective_ttl({}, None) == 300


def test_resolve_effective_ttl_from_payload_prompt_cache():
    assert statusline.resolve_effective_ttl({"prompt_cache": {"cache_ttl": "1h"}}, None) == 3600


def test_resolve_effective_ttl_from_top_level_cache_ttl():
    assert statusline.resolve_effective_ttl({"cache_ttl": 900}, None) == 900


def test_resolve_effective_ttl_from_config_toml(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('default_ttl = "1h"\n', encoding="utf-8")
    assert statusline.resolve_effective_ttl({}, config_dir) == 3600


def test_resolve_effective_ttl_payload_wins_over_config(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('default_ttl = "1h"\n', encoding="utf-8")
    assert statusline.resolve_effective_ttl({"cache_ttl": "5m"}, config_dir) == 300


def test_resolve_effective_ttl_missing_config_file_falls_back(tmp_path):
    assert statusline.resolve_effective_ttl({}, tmp_path / "does-not-exist") == 300


# -- main(): never raises, stdin handling ------------------------------------


def test_main_malformed_stdin_exits_0_with_fallback_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "claudeglass"


def test_main_empty_stdin_exits_0(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "claudeglass"


def test_main_full_payload_prints_line_and_logs_usage_row(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {
        "context_window": {"used_tokens": 10000},
        "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": "2026-09-18T20:00:00Z"}},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "ctx 10k | 5h 50%"

    from claudeglass.tools import log_usage
    csv_path = config_dir / "usage-log.csv"
    assert csv_path.exists()
    rows = log_usage.load_usage_log(csv_path)
    # S1-context-budget addition: a payload whose context_window also
    # carries used_tokens now logs a *second*, independent
    # "context_window" ground-truth row alongside the rate_limits row --
    # see statusline.py's module docstring.
    assert len(rows) == 2
    assert rows[0]["window"] == "five_hour"
    assert rows[1]["window"] == "context_window"


# -- v3-limits: tagging an exhausted usage-log row as "limit_hit" -----------


def test_tag_limit_hit_rows_overrides_source_at_100_pct():
    rows = [{"window": "five_hour", "used_percentage": 100.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "limit_hit"


def test_tag_limit_hit_rows_overrides_source_above_100_pct():
    rows = [{"window": "seven_day", "used_percentage": 103.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "limit_hit"


def test_tag_limit_hit_rows_leaves_source_below_100_pct():
    rows = [{"window": "five_hour", "used_percentage": 99.9, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_tag_limit_hit_rows_ignores_non_cap_windows():
    # spend_limit is a WINDOW_NAMES entry but not one of the two
    # account-wide-pause windows limits.py cross-checks against.
    rows = [{"window": "spend_limit", "used_percentage": 100.0, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_tag_limit_hit_rows_does_not_mutate_input_rows():
    original = {"window": "five_hour", "used_percentage": 100.0, "source": "statusline"}
    rows = [original]
    statusline._tag_limit_hit_rows(rows)
    assert original["source"] == "statusline"


def test_tag_limit_hit_rows_tolerates_non_numeric_used_percentage():
    rows = [{"window": "five_hour", "used_percentage": None, "source": "statusline"}]
    tagged = statusline._tag_limit_hit_rows(rows)
    assert tagged[0]["source"] == "statusline"


def test_main_full_window_logs_limit_hit_source(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 100}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0

    from claudeglass.tools import log_usage

    csv_path = config_dir / "usage-log.csv"
    rows = log_usage.load_usage_log(csv_path)
    five_hour_rows = [r for r in rows if r["window"] == "five_hour"]
    assert len(five_hour_rows) == 1
    assert five_hour_rows[0]["source"] == "limit_hit"


def test_main_partial_window_keeps_statusline_source(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 50}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0

    from claudeglass.tools import log_usage

    csv_path = config_dir / "usage-log.csv"
    rows = log_usage.load_usage_log(csv_path)
    five_hour_rows = [r for r in rows if r["window"] == "five_hour"]
    assert len(five_hour_rows) == 1
    assert five_hour_rows[0]["source"] == "statusline"


def test_main_explicit_config_dir_flag_wins_over_env_var(monkeypatch, capsys, tmp_path):
    """``--config-dir PATH`` (forwarded by cli.py's ``_cmd_statusline`` --
    see ``docs/api.md``-adjacent ``resolve_config_dir`` contract: "
    ``--config-dir`` wins; else ``$CLAUDE_CONFIG_DIR``; else
    ``~/.claude``") must actually be honoured, not silently dropped in
    favour of ``$CLAUDE_CONFIG_DIR`` -- a real v0.2 release bug where the
    CLI's own ``--help`` advertised the flag but ``main()`` never parsed
    it out of argv, so it always wrote to the env-var/home-dir location
    regardless of what the caller passed.
    """
    env_config_dir = tmp_path / "env-dir"
    explicit_config_dir = tmp_path / "explicit-dir"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_config_dir))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 5}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    rc = statusline.main(["--config-dir", str(explicit_config_dir)])

    assert rc == 0
    assert (explicit_config_dir / "usage-log.csv").exists()
    assert not (env_config_dir / "claudeglass" / "usage-log.csv").exists()
    assert not (env_config_dir / "usage-log.csv").exists()


def test_main_no_rate_limits_still_logs_context_window_row(monkeypatch, capsys, tmp_path):
    """S1-context-budget: a payload with no ``rate_limits`` at all still
    gets its own ``context_window`` row logged, independently of the
    rate_limits-driven append -- see statusline.py's module docstring."""
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"context_window": {"used_tokens": 1000}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    csv_path = config_dir / "usage-log.csv"
    assert csv_path.exists()

    from claudeglass import context_budget

    rows = context_budget.load_context_window_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["context_window_used_tokens"] == 1000


def test_main_no_context_window_and_no_rate_limits_does_not_create_usage_log(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    assert not (config_dir / "usage-log.csv").exists()


def test_main_exception_during_stdin_read_falls_back(monkeypatch, capsys):
    class _RaisingStdin:
        def read(self):
            raise OSError("boom")

    monkeypatch.setattr("sys.stdin", _RaisingStdin())
    rc = statusline.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "claudeglass"


def test_main_stdout_reconfigure_failure_is_swallowed(monkeypatch, capsys):
    """A stdout that doesn't support ``reconfigure`` at all (e.g. a plain
    ``io.StringIO`` swapped in by a stricter test/embedding harness than
    capsys) must not blank the status line."""
    class _NoReconfigureStdout(io.StringIO):
        def reconfigure(self, *args, **kwargs):
            raise AttributeError("no reconfigure on this stream")

    fake_stdout = _NoReconfigureStdout()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = statusline.main([])
    assert rc == 0
    assert fake_stdout.getvalue().strip() == "claudeglass"


def test_main_never_raises_when_print_itself_fails(monkeypatch, capsys):
    """``_safe_print`` must swallow a broken-pipe-style failure from
    ``print()`` (e.g. the host already closed stdout) rather than let it
    escape main() -- the WP6 "never blank the status line" contract
    applies to the print call itself, not just upstream parsing."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def _raising_print(*args, **kwargs):
        raise BrokenPipeError("downstream closed")

    monkeypatch.setattr("builtins.print", _raising_print)
    rc = statusline.main([])
    assert rc == 0


def test_safe_print_swallows_any_exception(capsys):
    class _Boom:
        def __str__(self):
            raise ValueError("boom")

    statusline._safe_print(_Boom())  # must not raise
    statusline._safe_print("fine")
    assert capsys.readouterr().out.strip() == "fine"


# -- print_install_fragment / --install flag ---------------------------------


#: The interpreter each platform's block names: this Python by full path
#: on the platform the tests run on (``py -3`` fails silently where the
#: launcher is missing), the generic form on the other.
_PY = "/opt/py/bin/python3"
_WIN_EXE = f'"{_PY}"' if os.name == "nt" else "py -3"
_POSIX_EXE = f'"{_PY}"' if os.name != "nt" else "python3"


def test_print_install_fragment_contains_both_platforms():
    text = statusline.print_install_fragment(python=_PY)
    assert "Windows:" in text
    assert "POSIX" in text
    windows_command, posix_command = _extract_commands(text)
    assert windows_command == f"{_WIN_EXE} -m claudeglass.statusline"
    assert posix_command == f"{_POSIX_EXE} -m claudeglass.statusline"
    assert '"statusLine"' in text
    # Each platform's JSON fragment must itself be valid JSON.
    for block in text.split("Windows:\n", 1)[1].split("\n\nPOSIX"):
        candidate = block.strip()
        if candidate.startswith("{"):
            json.loads(candidate)


def test_main_print_install_fragment_flag(monkeypatch, capsys):
    rc = statusline.main(["--print-install-fragment"])
    assert rc == 0
    assert "statusLine" in capsys.readouterr().out


def test_main_install_flag_alias(monkeypatch, capsys):
    rc = statusline.main(["--install"])
    assert rc == 0
    assert "statusLine" in capsys.readouterr().out


def _extract_commands(text: str) -> tuple[str, str]:
    """(windows_command, posix_command) parsed out of a
    ``print_install_fragment``-shaped report -- each platform's JSON
    block decoded properly rather than string-matched, since a Windows
    path's backslashes are JSON-escaped in the fragment text itself
    (``"C:\\\\...\\\\claudeglass.pyz"``).
    """
    _, _, rest = text.partition("Windows:\n")
    windows_json, _, posix_block = rest.partition("POSIX (Linux/macOS):\n")
    windows_command = json.loads(windows_json.strip())["statusLine"]["command"]
    posix_command = json.loads(posix_block.strip())["statusLine"]["command"]
    return windows_command, posix_command


def test_print_install_fragment_pyz_mode_uses_archive_path_not_dash_m(tmp_path):
    """When invoked from a ``.pyz`` build, ``python -m
    claudeglass.statusline`` does not work -- the package lives
    inside the archive, not on ``sys.path``. Passing ``pyz_path`` explicitly
    (mirroring ``installer.plan_service_install``'s own parameter) must
    produce the archive-path form instead, matching
    ``installer._serve_argv``'s ``[exe, str(pyz_path), *args]`` shape.
    """
    pyz_path = tmp_path / "claudeglass.pyz"
    text = statusline.print_install_fragment(pyz_path=pyz_path, python=_PY)

    assert "-m claudeglass.statusline" not in text
    windows_command, posix_command = _extract_commands(text)
    assert windows_command == f'{_WIN_EXE} "{pyz_path}" statusline'
    assert posix_command == f'{_POSIX_EXE} "{pyz_path}" statusline'


def test_print_install_fragment_pyz_mode_resolves_relative_path(tmp_path, monkeypatch):
    """An explicit ``pyz_path`` that isn't already absolute is still
    embedded as an absolute path -- the fragment ends up pasted into
    settings.json and run from an arbitrary working directory later."""
    monkeypatch.chdir(tmp_path)
    text = statusline.print_install_fragment(pyz_path=Path("claudeglass.pyz"), python=_PY)
    windows_command, posix_command = _extract_commands(text)
    abs_path = str((tmp_path / "claudeglass.pyz").resolve())
    assert windows_command == f'{_WIN_EXE} "{abs_path}" statusline'
    assert posix_command == f'{_POSIX_EXE} "{abs_path}" statusline'


def test_print_install_fragment_auto_detects_pyz_from_sys_argv(monkeypatch, tmp_path):
    """With no explicit ``pyz_path``, the fragment auto-detects the same
    way ``installer.detect_pyz_path``/``install-service`` already does --
    from ``sys.argv[0]`` -- so a plain ``claudeglass.pyz init`` run
    (which calls this with no arguments, see ``cli._cmd_init``) still gets
    the pyz-aware fragment without any extra wiring.
    """
    archive = tmp_path / "claudeglass.pyz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("__main__.py", "print('hi')\n")
    monkeypatch.setattr(installer.sys, "argv", [str(archive)])

    text = statusline.print_install_fragment(python=_PY)

    assert "-m claudeglass.statusline" not in text
    windows_command, posix_command = _extract_commands(text)
    abs_path = str(archive.resolve())
    assert windows_command == f'{_WIN_EXE} "{abs_path}" statusline'
    assert posix_command == f'{_POSIX_EXE} "{abs_path}" statusline'


def test_print_install_fragment_no_pyz_keeps_dash_m_form():
    text = statusline.print_install_fragment(pyz_path=None, python=_PY)
    windows_command, posix_command = _extract_commands(text)
    assert windows_command == f"{_WIN_EXE} -m claudeglass.statusline"
    assert posix_command == f"{_POSIX_EXE} -m claudeglass.statusline"


def test_install_command_names_this_python_by_full_path():
    # The fragment for this platform never depends on the py launcher or
    # a python3 on PATH.
    assert statusline.install_command(pyz_path=None, python=_PY) == f'"{_PY}" -m claudeglass.statusline'


# -- S1-exports: cache trailing CSV columns ----------------------------------


def test_append_context_window_row_writes_cache_columns(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    payload = {
        "session_id": "sess_cache",
        "prompt_cache": {
            "warm": True,
            "ttl": "5m",
            "expires_at": 1_800_000_300,
            "misses": 2,
            "last_miss_cause": {"causes": ["tools_changed"]},
            "recache_tokens_if_cold": 4000,
        },
    }
    now = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)

    with open(csv_path, encoding="utf-8", newline="") as fh:
        header = fh.readline().strip().split(",")
    assert header[:6] == list(statusline.log_usage.CSV_FIELDS)
    assert header[9:] == [
        "cache_warm",
        "cache_ttl_s",
        "cache_expires_in_s",
        "cache_misses",
        "cache_last_miss_cause",
        "cache_recache_tokens_if_cold",
        "cache_miss_causes",
    ]

    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess_cache"
    assert row["cache_warm"] is True
    assert row["cache_ttl_s"] == 300
    assert row["cache_expires_in_s"] == 300  # expires_at - now, both fixed above
    assert row["cache_misses"] == 2
    assert row["cache_last_miss_cause"] == "tools"
    assert row["cache_recache_tokens_if_cold"] == 4000
    # A cache-only row carries no context-window data.
    assert row["context_window_used_tokens"] is None


def test_append_context_window_row_cache_only_payload_still_writes(tmp_path):
    """A payload with prompt_cache but no context_window at all must
    still get a row -- the "should I log?" gate is broadened to context
    OR cache data present, per the module docstring."""
    csv_path = tmp_path / "usage-log.csv"
    payload = {"session_id": "s1", "prompt_cache": {"warm": False}}
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)
    assert csv_path.exists()
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    assert rows[0]["cache_warm"] is False


def test_append_context_window_row_dedupes_on_cache_warm_change(tmp_path):
    """Per the task spec, a change in cache_warm alone counts as a new
    row even when the context-window columns are unchanged."""
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    base = {"session_id": "s1", "context_window": {"used_tokens": 100}}

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True}}, now)
    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1  # identical repeat is deduped

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": False}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 2  # cache_warm changed -> new row despite identical context columns


def test_append_context_window_row_dedupes_on_cache_misses_change(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    base = {"session_id": "s1", "context_window": {"used_tokens": 100}}

    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True, "misses": 1}}, now)
    statusline._append_context_window_row(csv_path, {**base, "prompt_cache": {"warm": True, "misses": 2}}, now)
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 2


def test_load_usage_log_ground_truth_tolerates_old_9_column_rows(tmp_path):
    """A file written by S1-context-budget alone (9 columns, no cache_*
    trailing columns yet) must be tolerated: cache fields simply read as
    ``None``."""
    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    statusline._append_context_window_row(
        csv_path, {"session_id": "old", "context_window": {"used_tokens": 55}}, now
    )
    rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "old"
    assert rows[0]["context_window_used_tokens"] == 55
    assert rows[0]["cache_warm"] is None
    assert rows[0]["cache_misses"] is None


def test_load_usage_log_ground_truth_missing_file_returns_empty(tmp_path):
    assert statusline.load_usage_log_ground_truth(tmp_path / "does-not-exist.csv") == []


def test_load_usage_log_ground_truth_ignores_non_ground_truth_rows(tmp_path):
    from claudeglass.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    log_usage_mod.append_rows(
        csv_path,
        [{"session_id": "s1", "window": "five_hour", "used_percentage": 42.0, "resets_at": ""}],
        source="statusline",
    )
    assert statusline.load_usage_log_ground_truth(csv_path) == []


# -- S1-exports: build_cache_ground_truth_table ------------------------------


def test_build_cache_ground_truth_table_empty_rows():
    table = statusline.build_cache_ground_truth_table(None)
    assert table.name == "cache_ground_truth"
    assert table.rows == []
    table2 = statusline.build_cache_ground_truth_table([])
    assert table2.rows == []


def test_build_cache_ground_truth_table_excludes_rows_without_cache_data():
    rows = [{"session_id": "s1", "cache_warm": None}]
    table = statusline.build_cache_ground_truth_table(rows)
    assert table.rows == []


def test_build_cache_ground_truth_table_summarises_per_session():
    rows = [
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": None, "cache_recache_tokens_if_cold": None},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 2, "cache_last_miss_cause": "ttl", "cache_recache_tokens_if_cold": 1000},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 3, "cache_last_miss_cause": "ttl", "cache_recache_tokens_if_cold": 3000},
        {"session_id": "s2", "cache_warm": True, "cache_misses": 0, "cache_last_miss_cause": None, "cache_recache_tokens_if_cold": None},
    ]
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}

    s1 = by_session["s1"]
    assert s1[1] == 3  # rows_logged
    assert round(s1[2], 3) == round(100.0 / 3, 3)  # warm_share: 1 of 3 warm
    assert s1[3] == 3  # misses: peak counter
    assert s1[4] == "ttl:2"
    assert s1[5] == 2000  # mean of 1000 and 3000

    s2 = by_session["s2"]
    assert s2[1] == 1
    assert s2[2] == 100.0
    assert s2[5] is None  # no recache values logged


# -- S1-exports (deliverable 2): report.build_report renders old + new format CSVs --


def test_report_cli_renders_with_old_and_new_format_usage_log(tmp_path):
    """cli.py's ``report`` command loads <config_dir>/usage-log.csv when
    present and passes it through to build_report -- both an old-format
    (S1-context-budget only, no cache_* columns) and a new-format
    (with cache_* columns) row must render the usage and context_budget
    tables without error."""
    from helpers import turn_line, write_jsonl

    from claudeglass import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-1.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20, cache_read_input_tokens=10) for i in range(3)],
    )

    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()

    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    # Old-format row (S1-context-budget: 9 columns, no cache_* columns).
    statusline._append_context_window_row(
        config_dir / "usage-log.csv", {"session_id": "old-sess", "context_window": {"used_tokens": 1000}}, now
    )
    # New-format row (S1-exports: adds the 6 cache_* trailing columns).
    statusline._append_context_window_row(
        config_dir / "usage-log.csv",
        {
            "session_id": "new-sess",
            "context_window": {"used_tokens": 2000},
            "prompt_cache": {"warm": True, "ttl": "5m", "misses": 1},
        },
        now,
    )

    rc = cli_mod.main(
        [
            "report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
        ]
    )
    assert rc == 0


def test_report_cli_scopes_cache_ground_truth_to_the_report_window(tmp_path, capsys):
    """Regression test for review finding 8 (should-fix): cache_ground_truth
    used to include every ground-truth row ever logged, from every
    session, ignoring the invocation's own --days/--since/--until window.
    Two rows logged for the same session -- one just now, one over a
    year ago -- must only count the recent one once the report is
    scoped to a recent --days window.
    """
    from helpers import turn_line, write_jsonl

    from claudeglass import cli as cli_mod

    projects_root = tmp_path / "projects"
    project_dir = projects_root / "proj-a"
    project_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    write_jsonl(project_dir / "s1.jsonl", [turn_line(timestamp=now_iso, input_tokens=100)])

    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    csv_path = config_dir / "usage-log.csv"

    old = now - timedelta(days=400)
    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "prompt_cache": {"warm": True, "misses": 1}}, old
    )
    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "prompt_cache": {"warm": False, "misses": 2}}, now
    )
    # Sanity: both rows really did land on disk before scoping.
    all_rows = statusline.load_usage_log_ground_truth(csv_path)
    assert len(all_rows) == 2

    rc = cli_mod.main(
        [
            "report",
            "--projects-root",
            str(projects_root),
            "--all-projects",
            "--config-dir",
            str(config_dir),
            "--days",
            "30",
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    usage_section = next(s for s in payload["report"]["sections"] if s["key"] == "usage")
    cache_table = next(t for t in usage_section["tables"] if t["name"] == "cache_ground_truth")
    by_session = {row[0]: row for row in cache_table["rows"]}
    # Only the recent row (inside the 30-day window) counts -- the
    # year-old row must be excluded, so rows_logged is 1, not 2.
    assert by_session["s1"][1] == 1


# -- review finding 3: statusline length bound -------------------------------


def test_render_status_bounds_line_length_against_huge_expires_at():
    """Regression test for review finding 3 (should-fix): an unbounded
    ``expires_at`` (e.g. 1e308) used to produce a 300+ character line by
    feeding straight into arithmetic with no clamp. render_status must
    now stay at or under statusline._MAX_LINE_LEN for every payload."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": 1e308}}
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN


def test_render_status_bounds_line_length_against_huge_recache_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": False, "recache_tokens_if_cold": 1e300}}
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    # Capped at _MAX_RECACHE_TOKENS before formatting, not left as 1e300.
    assert "recache ~10000k tokens" in line


def test_render_status_treats_huge_expires_at_as_epoch_milliseconds():
    """A payload shaped like ``(now + 252) * 1000`` (a plausible epoch-ms
    variant per the module docstring, not just adversarial input) must
    degrade to a sane countdown rather than a multi-digit garbage value."""
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    expires_at_ms = (now.timestamp() + 252) * 1000
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": expires_at_ms}}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache warm 5m 04:12"
    assert len(line) <= statusline._MAX_LINE_LEN


def test_render_status_full_line_never_exceeds_max_len_with_all_segments_hostile():
    """Every segment hostile at once -- the assembled line (even after
    per-segment clamps) must still respect the hard cap, and must never
    contain an embedded newline (finding 4's "one line" guarantee)."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {"used_tokens": 999999999},
        "prompt_cache": {"warm": True, "ttl": "5m\nEVIL", "expires_at": 1e308},
        "rate_limits": {
            "five_hour": {"used_percentage": 37},
            "seven_day": {"used_percentage": 12},
        },
    }
    line = statusline.render_status(payload, now, 300)
    assert len(line) <= statusline._MAX_LINE_LEN
    assert "\n" not in line
    assert "\r" not in line


# -- review finding 4: ttl injection -----------------------------------------


def test_render_status_ttl_with_embedded_newline_never_emits_second_line():
    """Regression test for review finding 4 (should-fix): a ``ttl`` string
    containing a newline used to be echoed verbatim, breaking the "one
    line" contract. It must now fall back to the numeric/label handling
    (or "?"), never carry the newline through."""
    now = datetime(2026, 9, 18, 12, 5, 0, tzinfo=timezone.utc)
    payload = {
        "prompt_cache": {"warm": True, "ttl": "5m\nEVIL SECOND LINE", "expires_at": now.timestamp() + 252},
    }
    line = statusline.render_status(payload, now, 300)
    assert "\n" not in line
    assert "EVIL" not in line


def test_render_status_ttl_non_shape_string_falls_back_to_placeholder():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "not-a-real-ttl-value-that-is-way-too-long"}}
    line = statusline.render_status(payload, now, 300)
    assert line == "cache warm ?"


def test_render_status_ttl_valid_shape_string_still_echoed():
    """A ``ttl`` matching ``^\\d+[smh]$`` is still accepted and echoed --
    the hardening only rejects everything else."""
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "900s"}}
    assert statusline.render_status(payload, now, 300) == "cache warm 900s"


# -- nit 13: expired warm countdown renders "expiring", not stuck 00:00 -----


def test_render_status_warm_countdown_past_expiry_renders_expiring():
    now = datetime(2026, 9, 18, 12, 10, 0, tzinfo=timezone.utc)
    payload = {"prompt_cache": {"warm": True, "ttl": "5m", "expires_at": now.timestamp() - 30}}
    assert statusline.render_status(payload, now, 300) == "cache warm 5m expiring"


# -- review finding 5: top_miss_causes from the wire's cumulative counts ----


def test_cache_row_values_reads_miss_causes_cumulative_counts():
    """``prompt_cache.miss_causes`` (the wire's own cumulative
    per-cause counts) is mapped through the same short-token allowlist
    and formatted as a compact ``cause:count;cause:count`` string."""
    payload = {
        "prompt_cache": {
            "warm": True,
            "miss_causes": {"tools_changed": 3, "ttl_expired_5m": 2, "some_unknown_cause": 1},
        }
    }
    values = statusline._cache_row_values(payload)
    assert values is not None
    miss_causes_str = values[6]
    assert miss_causes_str == "other:1;tools:3;ttl:2"


def test_build_cache_ground_truth_table_uses_last_row_cumulative_miss_causes():
    """Regression test for review finding 5 (should-fix): the *wire's*
    cumulative cache_miss_causes snapshot must be taken from the last row
    per session (an overwrite), not summed once per logged row -- summing
    would double the true counts across repeated refreshes."""
    rows = [
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
    ]
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}
    assert by_session["s1"][4] == "ttl:1"  # not "ttl:3"


def test_build_cache_ground_truth_table_falls_back_to_counting_genuine_misses():
    """Reproduces the review's own repro: one real miss (cause 'ttl'),
    followed by nine quiet warm turns where prompt_cache.last_miss_cause
    stays sticky (still 'ttl') but cache_misses does not increase, and no
    row in the session ever carries cache_miss_causes data at all (the
    old-format-log fallback case). top_miss_causes must report 'ttl:1',
    matching the misses column beside it -- not 'ttl:10' from naively
    counting the sticky field once per row."""
    rows = [{"session_id": "s1", "cache_warm": True, "cache_misses": 0, "cache_last_miss_cause": None}]
    rows.append({"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": "ttl"})
    for _ in range(8):
        rows.append({"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_last_miss_cause": "ttl"})
    table = statusline.build_cache_ground_truth_table(rows)
    by_session = {row[0]: row for row in table.rows}
    s1 = by_session["s1"]
    assert s1[1] == 10  # rows_logged
    assert s1[3] == 1  # misses: peak counter
    assert s1[4] == "ttl:1"  # not "ttl:10"


# -- review finding 6: usage-log header upgrade ------------------------------


def test_append_context_window_row_upgrades_a_legacy_6_column_header(tmp_path):
    """Regression test for review finding 6 (should-fix): a file created
    by log_usage.append_rows first (6-column CSV_FIELDS header) followed
    by a ground-truth row appended positionally used to leave a
    16-column row sitting under a 6-column header forever. Appending a
    ground-truth row must now upgrade the header once, atomically,
    padding every existing row out to the new width."""
    from claudeglass.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    # Simulate the real main() ordering: append_rows creates the file
    # with the plain 6-column header and one rate_limits row first.
    log_usage_mod.append_rows(
        csv_path,
        [{"session_id": "s1", "window": "five_hour", "used_percentage": 37.0, "resets_at": "x"}],
        source="statusline",
        now=now,
    )
    with open(csv_path, encoding="utf-8", newline="") as fh:
        header_before = fh.readline().strip().split(",")
    assert len(header_before) == 6

    statusline._append_context_window_row(
        csv_path, {"session_id": "s1", "context_window": {"used_tokens": 5000}}, now
    )

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        import csv as _csv

        rows = list(_csv.reader(fh))
    header_after = rows[0]
    assert header_after == list(statusline._GROUND_TRUTH_HEADER)
    # The pre-existing rate_limits row was padded out to the new width,
    # not truncated or dropped.
    legacy_row = rows[1]
    assert len(legacy_row) == len(header_after)
    assert legacy_row[:6] == [now.isoformat().replace("+00:00", "Z"), "s1", "five_hour", "37.0", "x", "statusline"]
    assert legacy_row[6:] == [""] * (len(header_after) - 6)

    # The new ground-truth row landed after the (now-padded) legacy row,
    # with its own real values.
    new_row = rows[2]
    assert new_row[2] == "context_window"
    assert new_row[6] == "5000.0" or new_row[6] == "5000"


def test_load_usage_log_upgraded_file_is_readable_by_dict_reader_without_none_key(tmp_path):
    """Fix for review finding 6's defence-in-depth (log_usage.py's
    ``restkey="_extra"``): even before any header upgrade runs, a
    ground-truth row with more fields than a legacy 6-column header
    must not corrupt log_usage.load_usage_log's dict rows with a
    literal ``None`` key -- the overflow lands under "_extra" instead.
    """
    from claudeglass.tools import log_usage as log_usage_mod

    csv_path = tmp_path / "usage-log.csv"
    # Write a legacy-shaped header directly, then a longer row under it,
    # without going through _ensure_ground_truth_header, to exercise the
    # reader's own defence independently of the writer-side fix.
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("logged_at,session_id,window,used_percentage,resets_at,source\n")
        fh.write("2026-09-18T00:00:00Z,s1,context_window,,,statusline,5000,,,1\n")

    rows = log_usage_mod.load_usage_log(csv_path)
    assert len(rows) == 1
    assert None not in rows[0]
    assert rows[0]["_extra"] == ["5000", "", "", "1"]


# -- nit 16: context_window field-name fallbacks -----------------------------


def test_render_status_ctx_segment_falls_back_to_total_input_tokens():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"context_window": {"total_input_tokens": 42000}}
    assert statusline.render_status(payload, now, 300) == "ctx 42k"


def test_render_status_ctx_segment_falls_back_to_current_usage_sum():
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {
        "context_window": {
            "current_usage": {
                "input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 250,
            }
        }
    }
    assert statusline.render_status(payload, now, 300) == "ctx 2k"


def test_context_window_row_values_field_name_fallbacks():
    payload = {
        "session_id": "s1",
        "context_window": {
            "total_input_tokens": 1000,
            "total_tokens": 200000,
            "remaining_percentage": 75,
        },
    }
    values = statusline._context_window_row_values(payload)
    assert values is not None
    session_id, used_percentage, used_tokens, size, cache_read_tokens = values
    assert used_tokens == 1000
    assert size == 200000
    assert used_percentage == 25  # 100 - remaining_percentage


def test_context_window_size_falls_back_to_size_field():
    assert statusline._context_window_size({"size": 100000}) == 100000


def test_cache_read_tokens_field_reads_current_usage(tmp_path):
    """SIG-5: column 9 used to look for an undocumented "autocompact"
    key that never appears on a real payload -- it's repurposed to
    ``current_usage.cache_read_input_tokens`` instead, same position."""
    assert statusline._cache_read_tokens_field({"current_usage": {"cache_read_input_tokens": 4000}}) == 4000
    assert statusline._cache_read_tokens_field({"current_usage": None}) is None  # before the first API call
    assert statusline._cache_read_tokens_field({}) is None
    # The dead field, if a payload somehow still carried it, is ignored.
    assert statusline._cache_read_tokens_field({"autoCompactThreshold": 155_000}) is None


# -- SIG-5: tail reads and retention pruning ---------------------------------


def _write_ground_truth_csv(csv_path: Path, rows: list[list]) -> None:
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(statusline._GROUND_TRUTH_HEADER)
        writer.writerows(rows)


def test_last_context_window_key_finds_a_row_within_the_tail_window(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    _write_ground_truth_csv(
        csv_path,
        [
            ["2026-09-01T00:00:00Z", "s1", "context_window", "10.0", "", "statusline",
             "111", "222", "", "0", "100", "50", "0", "", "", ""],
            ["2026-09-01T00:00:01Z", "s2", "context_window", "50.0", "", "statusline",
             "1000", "2000", "", "1", "300", "250", "3", "ttl", "40", "ttl:3"],
        ],
    )
    key = statusline._last_context_window_key(csv_path)
    assert key == ("s2", 50.0, 1000.0, 2000.0, None, 1.0, 3.0, "ttl:3")


def test_last_context_window_key_is_bounded_to_the_tail_window(tmp_path):
    """SIG-5: this now scans only the final _TAIL_BYTES, backwards --
    not the whole file. A context-window row sitting further back than
    that is invisible to a fresh scan (the documented trade-off: a row
    that could have been deduped gets appended again instead), whereas
    the old full-file forward scan would still have found it."""
    csv_path = tmp_path / "usage-log.csv"
    old_row = [
        "2026-09-01T00:00:00Z", "s1", "context_window", "50.0", "", "statusline",
        "1000", "2000", "", "1", "300", "250", "0", "", "", "",
    ]
    filler = ["2026-09-01T00:00:01Z", "filler", "five_hour", "10.0", "r", "statusline"] + [""] * 10
    _write_ground_truth_csv(csv_path, [old_row] + [filler] * 4000)
    assert csv_path.stat().st_size > statusline._TAIL_BYTES

    assert statusline._last_context_window_key(csv_path) is None


def test_ensure_ground_truth_header_already_full_width_is_a_noop_even_when_large(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    row = ["2026-09-01T00:00:00Z", "s1", "five_hour", "10.0", "r", "statusline"] + [""] * 10
    _write_ground_truth_csv(csv_path, [row] * 4000)
    assert csv_path.stat().st_size > statusline._TAIL_BYTES
    text_before = csv_path.read_text(encoding="utf-8")

    statusline._ensure_ground_truth_header(csv_path)

    assert csv_path.read_text(encoding="utf-8") == text_before


def test_ensure_ground_truth_header_short_header_upgrades_a_large_file(tmp_path):
    """SIG-5: the width check now peeks at only the first line, but the
    migration itself -- when one is actually needed -- still reads and
    rewrites the whole file correctly, however large."""
    csv_path = tmp_path / "usage-log.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("logged_at,session_id,window,used_percentage,resets_at,source\n")
        for i in range(4000):
            fh.write(f"2026-09-01T00:00:{i % 60:02d}Z,s1,five_hour,10.0,r,statusline\n")
    assert csv_path.stat().st_size > statusline._TAIL_BYTES

    statusline._ensure_ground_truth_header(csv_path)

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(statusline._GROUND_TRUTH_HEADER)
    assert len(rows) == 4001
    assert all(len(r) == len(statusline._GROUND_TRUTH_HEADER) for r in rows[1:])


# -- payload key-name recording -----------------------------------------


def test_record_payload_keys_writes_dotted_names_only(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    payload = {
        "context_window": {"used_tokens": 1000},
        "prompt_cache": {"warm": True, "expires_at": 123.0},
        "session_id": "sess-super-secret-value",
    }
    statusline.record_payload_keys(payload, config_dir)
    keys_path = config_dir / "statusline-keys.json"
    assert keys_path.exists()
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    assert set(data["keys"]) == {
        "context_window",
        "context_window.used_tokens",
        "prompt_cache",
        "prompt_cache.warm",
        "prompt_cache.expires_at",
        "session_id",
    }
    # Names only -- the secret-looking session id value must never appear.
    assert "sess-super-secret-value" not in keys_path.read_text(encoding="utf-8")


def test_record_payload_keys_does_not_rewrite_when_unchanged(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    payload = {"a": 1, "b": {"c": 2}}
    statusline.record_payload_keys(payload, config_dir)
    keys_path = config_dir / "statusline-keys.json"
    first_mtime = keys_path.stat().st_mtime_ns
    statusline.record_payload_keys(payload, config_dir)
    assert keys_path.stat().st_mtime_ns == first_mtime


def test_record_payload_keys_caps_at_max_recorded_keys(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    payload = {f"key_{i}": i for i in range(500)}
    statusline.record_payload_keys(payload, config_dir)
    data = json.loads((config_dir / "statusline-keys.json").read_text(encoding="utf-8"))
    assert len(data["keys"]) <= statusline._MAX_RECORDED_KEYS


def test_main_records_payload_keys(monkeypatch, capsys, tmp_path):
    config_dir = tmp_path / "claudeglass"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = {"context_window": {"used_tokens": 10000}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = statusline.main([])
    assert rc == 0
    keys_path = config_dir / "statusline-keys.json"
    assert keys_path.exists()
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    assert "context_window.used_tokens" in data["keys"]


# -- measured cache-miss causes (Cache tab) ----------------------------------


def test_build_measured_miss_causes_table_sums_final_counts_per_session():
    rows = [
        # s1 carries the cumulative field: only its last value counts.
        {"session_id": "s1", "cache_warm": True, "cache_misses": 1, "cache_miss_causes": "ttl:1"},
        {"session_id": "s1", "cache_warm": False, "cache_misses": 3, "cache_miss_causes": "tools:1;ttl:2"},
        # s2 has only the sticky last cause: one count per increase.
        {"session_id": "s2", "cache_warm": False, "cache_misses": 1, "cache_last_miss_cause": "ttl"},
        {"session_id": "s2", "cache_warm": False, "cache_misses": 1, "cache_last_miss_cause": "ttl"},
        {"session_id": "s2", "cache_warm": False, "cache_misses": 2, "cache_last_miss_cause": "sysprompt"},
    ]
    table = statusline.build_measured_miss_causes_table(rows)
    by_cause = {row[0]: row for row in table.rows}
    assert by_cause["ttl"][1] == 3 and by_cause["ttl"][3] == 2
    assert by_cause["tools"][1] == 1 and by_cause["sysprompt"][1] == 1
    assert round(sum(row[2] for row in table.rows), 6) == 100.0
    assert table.rows[0][0] == "ttl"  # most misses first
    assert table.value_labels["ttl"].startswith("Cache expired")


def test_build_measured_miss_causes_table_is_none_without_cause_data():
    assert statusline.build_measured_miss_causes_table(None) is None
    assert statusline.build_measured_miss_causes_table([{"session_id": "s1", "cache_warm": True, "cache_misses": 0}]) is None


def test_scoped_usage_log_rows_filters_sessions_and_window(tmp_path):
    assert statusline.scoped_usage_log_rows(tmp_path / "missing.csv", {"s1"}, None, None) is None
    rows = [
        {"session_id": "s1", "logged_at": "2026-09-20T10:00:00Z"},
        {"session_id": "s1", "logged_at": "2026-09-01T10:00:00Z"},
        {"session_id": "other", "logged_at": "2026-09-20T10:00:00Z"},
    ]
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    kept = [r for r in rows if r["session_id"] in {"s1"} and statusline.usage_log_row_in_window(r, since, None)]
    assert kept == [rows[0]]


# -- second line: feedback note and coaching hints --------------------------

from claudeglass.capture_catalogue import FEEDBACK_NOTE

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _capture_config(tmp_path, body):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(body, encoding="utf-8")
    return config_dir


def _lines(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert statusline.main([]) == 0
    return capsys.readouterr().out.splitlines()


def _jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _prompt(text="do it"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _reply(blocks, stop="tool_use", ts="2026-09-24T11:59:00Z"):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "stop_reason": stop, "content": blocks}}


def _use(tool_id, name="Bash"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _result(tool_id, chars):
    return {"type": "user", "toolUseResult": {}, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "x" * chars}]}}


def test_no_capture_config_prints_one_line(tmp_path, monkeypatch, capsys):
    assert _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 10000}}) == ["ctx 10k"]


def test_feedback_note_is_imported_lazily_not_on_every_prompt_refresh():
    # ROB-P10: statusline.py runs on every prompt refresh (its hot path);
    # capture_catalogue (and everything it pulls in) should only load
    # when a feedback note is actually about to be shown, not eagerly at
    # module import time.
    assert not hasattr(statusline, "FEEDBACK_NOTE")


def test_the_feedback_note_is_a_second_line_and_line_one_is_unchanged(tmp_path, monkeypatch, capsys):
    _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_skill", "feedback_note"]\n')
    lines = _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 10000}})
    assert lines == ["ctx 10k", FEEDBACK_NOTE]
    assert "\x1b" not in lines[1] and len(lines[1]) <= 120


def test_feedback_without_the_note_or_a_malformed_config_adds_nothing(tmp_path, monkeypatch, capsys):
    _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_skill"]\n')
    assert _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 10000}}) == ["ctx 10k"]
    _capture_config(tmp_path, "[capture\nfeedback = ")
    assert _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 10000}}) == ["ctx 10k"]


def test_a_large_last_output_beats_the_note(tmp_path, monkeypatch, capsys):
    _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\ncoaching = ["coaching_line"]\n')
    transcript = _jsonl(tmp_path / "t.jsonl", [_prompt(), _reply([_use("a")]), _result("a", 40_000)])
    lines = _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 30000}, "transcript_path": transcript})
    assert lines[0].startswith("ctx 30k") and lines[1].startswith("last output ~10k: try quieter cmd")


def test_the_note_shows_when_no_hint_fires(tmp_path, monkeypatch, capsys):
    _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\ncoaching = ["coaching_line"]\n')
    transcript = _jsonl(tmp_path / "t.jsonl", [_prompt(), _reply([_use("a")]), _result("a", 400)])
    lines = _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 30000}, "transcript_path": transcript})
    assert lines[1] == FEEDBACK_NOTE


def test_coaching_alone_prints_nothing_extra_when_no_hint_fires(tmp_path, monkeypatch, capsys):
    _capture_config(tmp_path, '[capture]\ncoaching = ["coaching_line"]\n')
    assert _lines(tmp_path, monkeypatch, capsys, {"context_window": {"used_tokens": 10000}}) == ["ctx 10k"]


def test_hint_large_context_at_the_end_of_a_turn(tmp_path):
    tail = [_prompt(), _reply([{"type": "text", "text": "done"}], stop="end_turn")]
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 150_000}}, tail, NOW)
    assert hint is not None and hint[1] == "ctx 150k: new task? /clear first or it re-reads"
    # Mid-turn (Claude still working) it waits.
    assert statusline.coaching_hint({"context_window": {"used_tokens": 150_000}}, tail[:1] + [_reply([_use("a")])], NOW) is None


def test_hint_many_reads_counts_only_the_current_message(tmp_path):
    old = [_reply([_use(f"o{i}", "Read") for i in range(6)])]
    current = [_reply([_use(f"r{i}", "Read" if i % 2 else "Grep") for i in range(5)])] + [_result(f"r{i}", 800) for i in range(5)]
    hint = statusline.coaching_hint({}, [_prompt(), *old, _prompt("next"), *current], NOW)
    assert hint is not None and hint[1].startswith("5 reads this msg: try an Explore agent")
    fewer = [_reply([_use(f"r{i}", "Read") for i in range(4)])] + [_result(f"r{i}", 800) for i in range(4)]
    assert statusline.coaching_hint({}, [_prompt(), *old, _prompt("next"), *fewer], NOW) is None


def test_hint_cache_about_to_go_cold(tmp_path):
    expires = NOW.timestamp() + 40
    payload = {"context_window": {"used_tokens": 80_000}, "prompt_cache": {"warm": True, "ttl": "5m", "expires_at": expires}}
    hint = statusline.coaching_hint(payload, [], NOW)
    assert hint is not None and hint[1] == "cache cold in 40s: reply now or re-pay 80k"
    payload["prompt_cache"]["expires_at"] = NOW.timestamp() + 200
    assert statusline.coaching_hint(payload, [], NOW) is None
    # Estimated from the last reply's time when the payload has no cache block.
    tail = [_prompt(), _reply([_use("a")], ts="2026-09-24T11:55:30Z")]
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 80_000}}, tail, NOW)
    assert hint is not None and hint[1].startswith("cache cold in 30s")


def test_the_biggest_hint_wins(tmp_path):
    tail = [_prompt(), _reply([_use("a")]), _result("a", 140_000), _reply([{"type": "text", "text": "ok"}], stop="end_turn")]
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 120_000}}, tail, NOW)
    # The 35k-token output outweighs a quarter of the 120k context...
    assert hint is not None and hint[1].startswith("last output ~35k")
    # ...and a quarter of a 200k context outweighs a 10k output.
    tail[2] = _result("a", 40_000)
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 200_000}}, tail, NOW)
    assert hint is not None and hint[1].startswith("ctx 200k: new task?")


def _said(text, minutes_ago, **extra):
    stamp = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**_prompt(text), "timestamp": stamp, **extra}


def _at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_hint_small_requests_one_at_a_time():
    # Words don't matter: each short follow-up got a file change.
    tail = [_said("Build the settings page with a form and a header", 30), _reply([_use("a", "Write")], ts=_at(29)),
            _said("make the save button bigger", 15), _reply([_use("b", "Edit")], ts=_at(14)),
            _said("<command-name>/cost</command-name>", 12), _said("[Request interrupted by user]", 11),
            _said("now move the logo left", 10), _reply([_use("c", "Edit")], ts=_at(9)),
            _said("and the footer text too", 1)]
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 60_000}}, tail, NOW)
    assert hint == (30_000, "3 small asks in a row: plan them as one prompt", "drip_feed")
    # A reply that changed no file breaks the run...
    no_edit = [*tail[:7], _reply([_use("c", "Bash")], ts=_at(9)), tail[8]]
    assert statusline.coaching_hint({}, no_edit, NOW) is None
    # ...and so does a detailed message planning several changes at once.
    detailed = [*tail[:6], _said("Now: " + "move the logo left, grey footer, wider form; " * 8, 10), *tail[7:]]
    assert statusline.coaching_hint({}, detailed, NOW) is None
    # A thank-you isn't another request.
    assert statusline.coaching_hint({}, [*tail[:8], _said("thanks!", 1)], NOW) is None


def test_hint_stopping_claude_again_and_again():
    stop = "[Request interrupted by user]"
    tail = [_said("Refactor the store", 30), _said(stop, 18), _said("no, keep the API", 17),
            _said(stop, 9), _said("use the cache", 8), _said(stop, 1), _said(stop, 1, isSidechain=True)]
    hint = statusline.coaching_hint({}, tail, NOW)
    assert hint is not None and hint[1] == "stopped 3x in 20m: agree a plan first (Shift+Tab)" and hint[2] == "stop_loop"
    assert statusline.coaching_hint({}, [tail[0], _said(stop, 25), *tail[2:5]], NOW) is None


def test_hint_a_huge_message():
    hint = statusline.coaching_hint({}, [_said("Why does this fail?\n" + "log line\n" * 5_000, 1)], NOW)
    assert hint is not None and hint[1] == "msg ~11k: paste less, or give a file path" and hint[2] == "big_paste"
    assert statusline.coaching_hint({}, [_said("log line\n" * 1_000, 1)], NOW) is None


def test_a_hint_is_capped_at_the_ux5_budget_even_for_a_huge_number(tmp_path):
    tail = [_prompt(), _reply([{"type": "text", "text": "done"}], stop="end_turn")]
    hint = statusline.coaching_hint({"context_window": {"used_tokens": 123_456_789}}, tail, NOW)
    assert hint is not None and len(hint[1]) <= statusline._MAX_HINT_LEN


def test_capture_lines_is_off_past_its_until(tmp_path):
    config_dir = _capture_config(
        tmp_path, '[capture]\nfeedback = ["feedback_note"]\ncoaching = ["coaching_line"]\nuntil = "2026-09-24T11:00:00Z"\n'
    )
    assert statusline._capture_lines(config_dir, NOW) == (False, False)  # NOW is 12:00, past 11:00
    config_dir = _capture_config(
        tmp_path, '[capture]\nfeedback = ["feedback_note"]\ncoaching = ["coaching_line"]\nuntil = "2026-09-24T13:00:00Z"\n'
    )
    assert statusline._capture_lines(config_dir, NOW) == (True, True)
    # No "until" at all: on, same as before this check existed.
    config_dir = _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\ncoaching = ["coaching_line"]\n')
    assert statusline._capture_lines(config_dir, NOW) == (True, True)


def test_second_line_is_silent_past_the_capture_until(tmp_path):
    config_dir = _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\nuntil = "2026-09-24T11:00:00Z"\n')
    assert statusline.second_line({"session_id": "s1"}, config_dir, NOW) is None


def _ctx_payload(tokens: int, session_id: str = "s1") -> dict:
    return {"session_id": session_id, "context_window": {"used_tokens": tokens}}


def _clear_hint_payload(tmp_path, tokens: int, session_id: str = "s1") -> dict:
    tail = [_prompt(), _reply([{"type": "text", "text": "done"}], stop="end_turn")]
    return {**_ctx_payload(tokens, session_id), "transcript_path": _jsonl(tmp_path / "t.jsonl", tail)}


def test_hint_cooldown_suppresses_the_same_kind_then_lifts(tmp_path):
    """UX-5: a hint kind that just showed for this session is suppressed
    on the very next refresh, but can show again once its cooldown
    elapses -- otherwise it would repeat on every single prompt."""
    config_dir = _capture_config(tmp_path, '[capture]\ncoaching = ["coaching_line"]\n')
    payload = _clear_hint_payload(tmp_path, 150_000)
    first = statusline.second_line(payload, config_dir, NOW)
    assert first == "ctx 150k: new task? /clear first or it re-reads"
    soon = NOW.replace(second=1)
    assert statusline.second_line(payload, config_dir, soon) is None
    later = datetime.fromtimestamp(NOW.timestamp() + statusline._HINT_COOLDOWN_S + 1, tz=timezone.utc)
    assert statusline.second_line(payload, config_dir, later) == first


def test_hint_hysteresis_rearms_early_once_the_stake_grows_enough(tmp_path):
    """UX-5: a hint kind on cooldown can still interrupt it once its
    stake has grown past _HINT_REARM_FACTOR times what it was last
    time -- a merely flickering value cannot."""
    config_dir = _capture_config(tmp_path, '[capture]\ncoaching = ["coaching_line"]\n')
    first = statusline.second_line(_clear_hint_payload(tmp_path, 150_000), config_dir, NOW)
    assert first == "ctx 150k: new task? /clear first or it re-reads"
    soon = NOW.replace(second=1)
    # 160k -> stake 40000, only ~1.07x the 37500 that just fired: still suppressed.
    assert statusline.second_line(_clear_hint_payload(tmp_path, 160_000), config_dir, soon) is None
    # 250k -> stake 62500, ~1.67x: past the 1.5x hysteresis margin, fires early.
    assert statusline.second_line(_clear_hint_payload(tmp_path, 250_000), config_dir, soon) == (
        "ctx 250k: new task? /clear first or it re-reads"
    )


def test_feedback_note_shows_once_per_session_then_stops(tmp_path):
    config_dir = _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\n')
    assert statusline.second_line({"session_id": "s1"}, config_dir, NOW) == FEEDBACK_NOTE
    later = datetime.fromtimestamp(NOW.timestamp() + 10_000, tz=timezone.utc)
    assert statusline.second_line({"session_id": "s1"}, config_dir, later) is None
    # A different session starts with none of that state.
    assert statusline.second_line({"session_id": "s2"}, config_dir, later) == FEEDBACK_NOTE


def test_gating_is_skipped_without_a_session_id(tmp_path):
    """No ``session_id`` means nowhere to key the state file, so the note
    shows every time rather than being silently dropped -- same as
    before this cooldown/once-per-session state existed."""
    config_dir = _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\n')
    assert statusline.second_line({}, config_dir, NOW) == FEEDBACK_NOTE
    assert statusline.second_line({}, config_dir, NOW) == FEEDBACK_NOTE


def test_state_file_survives_being_missing_or_corrupt(tmp_path):
    config_dir = _capture_config(tmp_path, '[capture]\nfeedback = ["feedback_note"]\n')
    (config_dir / statusline._STATE_FILENAME).write_text("not json", encoding="utf-8")
    assert statusline.second_line({"session_id": "s1"}, config_dir, NOW) == FEEDBACK_NOTE


# -- SIG-4: the statusline's own cost/recache ground truth -------------------


def _sig4_config(tmp_path, level="free", until=None):
    body = f'[capture]\nlevel = "{level}"\n'
    if until:
        body += f'until = "{until}"\n'
    return _capture_config(tmp_path, body)


def test_ground_truth_values_reads_cost_and_recache():
    cost, recache = statusline._ground_truth_values(
        {"cost": {"total_cost_usd": 0.5}, "prompt_cache": {"recache_tokens_if_cold": 2000}}
    )
    assert cost == 0.5 and recache == 2000
    assert statusline._ground_truth_values({}) == (None, None)


def test_ground_truth_signal_is_salted_numbers_only_and_gated_by_level(tmp_path):
    from claudeglass import signals
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path)
    salt = load_or_create_salt(config_dir)
    payload = {
        "session_id": "s1",
        "cost": {"total_cost_usd": 1.2345678},
        "prompt_cache": {"recache_tokens_if_cold": 45000},
    }
    statusline._write_ground_truth_signal(config_dir, payload, NOW)
    path = signals.signals_dir(config_dir) / "2026-09.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    cost_line = next(r for r in lines if r["e"] == "cost")
    recache_line = next(r for r in lines if r["e"] == "recache")
    # Numbers only: no field beyond the timestamp, the salted id and the
    # event's own number.
    assert set(cost_line) == {"ts", "sid", "e", "usd"}
    assert set(recache_line) == {"ts", "sid", "e", "tokens"}
    assert cost_line["sid"] == recache_line["sid"] == signals.session_hash(salt, "s1")
    assert cost_line["sid"] != "s1"
    assert cost_line["usd"] == pytest.approx(1.2345678, rel=1e-6)
    assert recache_line["tokens"] == 45000


def test_ground_truth_signal_is_a_noop_when_capture_is_off(tmp_path):
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path, level="off")
    load_or_create_salt(config_dir)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 1.0}}, NOW)
    assert not (config_dir / "signals").exists()


def test_ground_truth_signal_respects_capture_until(tmp_path):
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path, until="2026-09-24T11:00:00Z")
    load_or_create_salt(config_dir)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 1.0}}, NOW)
    assert not (config_dir / "signals").exists()


def test_ground_truth_signal_never_creates_the_salt(tmp_path):
    config_dir = _sig4_config(tmp_path)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 1.0}}, NOW)
    assert not (config_dir / "salt").exists()
    assert not (config_dir / "signals").exists()


def test_ground_truth_signal_skips_a_payload_with_no_numbers_to_report(tmp_path):
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path)
    load_or_create_salt(config_dir)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1"}, NOW)
    assert not (config_dir / "signals").exists()


def test_ground_truth_signal_is_throttled_per_session_then_lifts(tmp_path):
    from claudeglass import signals
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path)
    load_or_create_salt(config_dir)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 1.0}}, NOW)
    soon = NOW.replace(second=1)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 2.0}}, soon)
    path = signals.signals_dir(config_dir) / "2026-09.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["usd"] == 1.0
    later = datetime.fromtimestamp(NOW.timestamp() + statusline._SIG4_THROTTLE_S + 1, tz=timezone.utc)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 3.0}}, later)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[1]["usd"] == 3.0


def test_ground_truth_signal_rotates_by_month(tmp_path):
    from claudeglass import signals
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path)
    load_or_create_salt(config_dir)
    statusline._write_ground_truth_signal(config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 1.0}}, NOW)
    next_month = NOW.replace(month=10, day=1)
    statusline._write_ground_truth_signal(
        config_dir, {"session_id": "s1", "cost": {"total_cost_usd": 2.0}}, next_month
    )
    folder = signals.signals_dir(config_dir)
    assert sorted(p.name for p in folder.iterdir()) == ["2026-09.jsonl", "2026-10.jsonl"]


def test_main_writes_the_ground_truth_signal_end_to_end(tmp_path, monkeypatch, capsys):
    from claudeglass import signals
    from claudeglass.parse import load_or_create_salt

    config_dir = _sig4_config(tmp_path)
    salt = load_or_create_salt(config_dir)
    payload = {
        "session_id": "s1",
        "context_window": {"used_tokens": 1000},
        "cost": {"total_cost_usd": 0.42},
    }
    _lines(tmp_path, monkeypatch, capsys, payload)
    # main() stamps its own now() -- just check today's month file got a
    # correctly-salted cost line, not an exact timestamp.
    from datetime import datetime as _dt

    path = signals.signals_dir(config_dir) / f"{_dt.now(timezone.utc).strftime('%Y-%m')}.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines == [{"ts": lines[0]["ts"], "sid": signals.session_hash(salt, "s1"), "e": "cost", "usd": 0.42}]


def test_a_reply_stamped_ahead_of_the_clock_never_shows_more_than_the_ttl(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, "2026-09-24T12:10:00Z")
    line = statusline.render_status({"transcript_path": str(transcript)}, NOW, 300)
    assert line == "cache est 5m 05:00"

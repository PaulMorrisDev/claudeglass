"""``config.py``'s ``[capture]`` table: loading and validating it,
:func:`set_capture` (presets, one-by-one metrics, the on/off stamps, the
change log) and the atomic, table-keeping ``config.toml`` writes it
relies on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import capture_catalogue
from claudeglass.config import (
    CAPTURE_LOG_NAME,
    SIGNAL_RETENTION_DEFAULT_DAYS,
    CaptureConfig,
    ConfigError,
    load_capture_log,
    load_config,
    prune_capture_log,
    set_capture,
    write_config_values,
)

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)


def _write(tmp_path, text: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")


def test_capture_is_off_by_default(tmp_path):
    capture = load_config(config_dir=tmp_path).capture
    assert capture == CaptureConfig()
    assert not capture.is_on and capture.active_metrics() == ()


def test_a_capture_table_is_loaded(tmp_path):
    _write(tmp_path, """
[capture]
level = "custom"
metrics = ["task", "fit"]
sample = 25
until = "2026-10-01"
projects = ["claudeglass", "!secret"]
feedback = ["feedback_skill", "feedback_note"]
coaching = ["coaching_line"]
enabled_at = "2026-09-24T06:00:00+00:00"
""")
    capture = load_config(config_dir=tmp_path).capture
    assert capture.level == "custom" and capture.sample == 25
    assert capture.active_metrics() == ("task", "result", "fit", "feedback_skill", "feedback_note")
    assert capture.projects == ["claudeglass", "!secret"]
    assert not capture.expired(NOW)
    assert capture.expired(datetime(2026, 10, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("body, message", [
    ('capture = "on"', "'capture' must be a table"),
    ('[capture]\nlevel = "max"', "'capture.level'"),
    ('[capture]\nsample = 30', "'capture.sample'"),
    ('[capture]\nsample = true', "'capture.sample'"),
    ('[capture]\nmetrics = ["task", "mood"]', "'mood'"),
    ('[capture]\nmetrics = ["coaching_line"]', "'coaching_line'"),
    ('[capture]\nuntil = "next week"', "'capture.until'"),
    ('[capture]\nprojects = ["("]', "bad pattern"),
    ('[capture]\nfeedback = ["survey"]', "'survey'"),
    ('[capture]\ncoaching = "yes"', "must be a list"),
])
def test_a_bad_capture_table_is_named(tmp_path, body, message):
    _write(tmp_path, body)
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir=tmp_path)


def test_turning_capture_on_stamps_it_and_logs_the_change(tmp_path):
    capture = set_capture(tmp_path, level="essentials", now=NOW)
    assert capture.level == "essentials" and capture.enabled_at == "2026-09-24T06:00:00+00:00"
    # CAP-8: a fresh off -> on switch with no `until` given gets the
    # default time-box, so capture can't run forever unnoticed.
    assert capture.until == "2026-10-08T06:00:00+00:00"
    assert load_config(config_dir=tmp_path).capture == capture
    log = load_capture_log(tmp_path)
    assert log == [{
        "ts": "2026-09-24T06:00:00+00:00",
        "level": "essentials",
        "changed": {
            "level": {"from": "off", "to": "essentials"},
            "until": {"from": "", "to": "2026-10-08T06:00:00+00:00"},
        },
    }]


def test_set_capture_respects_an_explicit_no_limit_on_switch_on(tmp_path):
    # CAP-8: until="" is a deliberate "no limit", not "unset" -- it must
    # not be overridden by the default the way until=None would be.
    capture = set_capture(tmp_path, level="essentials", until="", now=NOW)
    assert capture.until == ""


def test_set_capture_does_not_reset_an_already_on_capture_s_until(tmp_path):
    # CAP-8: the default only applies to a fresh off -> on switch -- once
    # capture is already on, changing the level alone (no until given)
    # must not silently impose a new time-box over an existing choice
    # (here, an explicit "no limit" from the first call).
    set_capture(tmp_path, level="essentials", until="", now=NOW)
    later = set_capture(tmp_path, level="deep", now=NOW)
    assert later.until == ""


def test_a_change_that_changes_nothing_is_not_logged(tmp_path):
    set_capture(tmp_path, level="standard", now=NOW)
    set_capture(tmp_path, level="standard", now=NOW)
    assert len(load_capture_log(tmp_path)) == 1


def test_raising_the_level_keeps_the_first_on_stamp(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    later = set_capture(tmp_path, level="deep", now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert later.enabled_at == "2026-09-24T06:00:00+00:00"


def test_metrics_one_by_one_become_custom_or_the_preset_they_match(tmp_path):
    custom = set_capture(tmp_path, metrics=["task", "agent_brief"], now=NOW)
    assert custom.level == "custom" and custom.metrics == ["task", "result", "agent_brief"]
    preset = set_capture(tmp_path, metrics=list(capture_catalogue.level_metrics("standard")), now=NOW)
    assert preset.level == "standard" and preset.metrics == []
    assert set_capture(tmp_path, metrics=[], now=NOW).level == "off"


def test_switching_into_deep_turns_its_feedback_survey_on(tmp_path):
    deep = set_capture(tmp_path, level="deep", now=NOW)
    assert deep.feedback == list(capture_catalogue.DEEP_FEEDBACK_IDS)
    assert "feedback_reminder" in deep.active_metrics()
    # Added to what was already on, in catalogue order, and logged.
    other = tmp_path / "other"
    set_capture(other, level="standard", feedback=["dashboard_rating"], now=NOW)
    assert set_capture(other, level="deep", now=NOW).feedback == list(capture_catalogue.FEEDBACK_IDS)
    assert "feedback" in load_capture_log(other)[-1]["changed"]


def test_metrics_that_add_up_to_deep_turn_its_feedback_on_too(tmp_path):
    deep = set_capture(tmp_path, metrics=list(capture_catalogue.level_metrics("deep")), now=NOW)
    assert deep.level == "deep" and deep.feedback == list(capture_catalogue.DEEP_FEEDBACK_IDS)


def test_an_explicit_feedback_choice_wins_over_deep(tmp_path):
    assert set_capture(tmp_path, level="deep", feedback=[], now=NOW).feedback == []


def test_deep_s_feedback_stays_as_you_leave_it(tmp_path):
    set_capture(tmp_path, level="deep", now=NOW)
    # Leaving Deep keeps it.
    assert set_capture(tmp_path, level="standard", now=NOW).feedback == list(capture_catalogue.DEEP_FEEDBACK_IDS)
    # Turned off while at Deep, picking Deep again doesn't bring it back:
    # only a switch into Deep does.
    set_capture(tmp_path, level="deep", now=NOW)
    set_capture(tmp_path, feedback=[], now=NOW)
    assert set_capture(tmp_path, level="deep", now=NOW).feedback == []


def test_turning_capture_off_clears_the_stamp_and_the_end_date(tmp_path):
    set_capture(tmp_path, level="essentials", until="2026-10-01T00:00:00+00:00", now=NOW)
    off = set_capture(tmp_path, level="off", now=NOW)
    assert (off.level, off.enabled_at, off.until) == ("off", "", "")


def test_set_capture_keeps_the_rest_of_config_toml(tmp_path):
    _write(tmp_path, 'billing = "subscription"\n\n[savers]\nnames = ["rtk"]\n')
    set_capture(tmp_path, level="essentials", sample=50, feedback=["feedback_note", "feedback_skill"], now=NOW)
    config = load_config(config_dir=tmp_path)
    assert config.savers == ["rtk"]
    assert config.capture.sample == 50
    assert config.capture.feedback == ["feedback_skill", "feedback_note"]  # catalogue order


def test_set_capture_rejects_bad_values_before_writing(tmp_path):
    with pytest.raises(ConfigError):
        set_capture(tmp_path, metrics=["mood"], now=NOW)
    with pytest.raises(ConfigError):
        set_capture(tmp_path, level="essentials", sample=33, now=NOW)
    with pytest.raises(ConfigError):
        set_capture(tmp_path, level="essentials", coaching=["nagging"], now=NOW)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / CAPTURE_LOG_NAME).exists()


def test_set_capture_refuses_when_config_toml_cannot_be_rewritten_in_place(tmp_path):
    _write(tmp_path, '[thresholds]\nnested = { too = "deep" }\n')
    with pytest.raises(ConfigError, match="config.toml.new"):
        set_capture(tmp_path, level="essentials", now=NOW)
    assert "[capture]" not in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert not (tmp_path / CAPTURE_LOG_NAME).exists()


def test_the_dot_new_fallback_keeps_flat_tables(tmp_path):
    _write(tmp_path, '[savers]\nnames = ["rtk"]\n')
    path = write_config_values(tmp_path, {"capture": {"level": "free"}, "thresholds": {"nested": {"too": "deep"}}})
    assert path.name == "config.toml.new"
    text = path.read_text(encoding="utf-8")
    assert '[savers]\nnames = ["rtk"]' in text
    assert '[capture]\nlevel = "free"' in text
    assert "nested" not in text


def test_config_writes_leave_no_temporary_files(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    set_capture(tmp_path, level="off", now=NOW)
    assert sorted(p.name for p in tmp_path.iterdir()) == [CAPTURE_LOG_NAME, "config.toml"]


def test_an_unreadable_log_line_is_skipped(tmp_path):
    set_capture(tmp_path, level="essentials", now=NOW)
    with open(tmp_path / CAPTURE_LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write("not json\n[1, 2]\n")
    assert len(load_capture_log(tmp_path)) == 1
    assert load_capture_log(tmp_path / "missing") == []


# -- prune_capture_log (SEC-P8/G7: capture-log.jsonl was never pruned) -----


def _log_line(ts: datetime, level: str = "essentials") -> str:
    record = {"ts": ts.isoformat(timespec="seconds"), "level": level, "changed": {}}
    return json.dumps(record, sort_keys=True)


def test_prune_capture_log_removes_records_older_than_retention(tmp_path):
    old = NOW - timedelta(days=200)
    recent = NOW - timedelta(days=10)
    (tmp_path / CAPTURE_LOG_NAME).write_text(
        _log_line(old) + "\n" + _log_line(recent) + "\n", encoding="utf-8"
    )

    removed = prune_capture_log(tmp_path, retention_days=180, now=NOW)

    assert removed == 1
    log = load_capture_log(tmp_path)
    assert len(log) == 1 and log[0]["ts"] == recent.isoformat(timespec="seconds")


def test_prune_capture_log_keeps_everything_within_retention(tmp_path):
    (tmp_path / CAPTURE_LOG_NAME).write_text(
        _log_line(NOW - timedelta(days=5)) + "\n" + _log_line(NOW - timedelta(days=10)) + "\n",
        encoding="utf-8",
    )

    assert prune_capture_log(tmp_path, retention_days=180, now=NOW) == 0
    assert len(load_capture_log(tmp_path)) == 2


def test_prune_capture_log_on_a_missing_file_is_a_noop(tmp_path):
    assert prune_capture_log(tmp_path / "missing", retention_days=180, now=NOW) == 0


def test_prune_capture_log_also_drops_unparseable_lines(tmp_path):
    # A prune pass is also a chance to repair the file -- a line that
    # load_capture_log already treats as invisible (see
    # test_an_unreadable_log_line_is_skipped) doesn't survive a rewrite
    # either.
    (tmp_path / CAPTURE_LOG_NAME).write_text(
        _log_line(NOW) + "\nnot json\n[1, 2]\n", encoding="utf-8"
    )

    removed = prune_capture_log(tmp_path, retention_days=180, now=NOW)

    assert removed == 2
    assert len(load_capture_log(tmp_path)) == 1


def test_prune_capture_log_defaults_to_the_signal_retention_default(tmp_path):
    assert SIGNAL_RETENTION_DEFAULT_DAYS == 180
    just_over = NOW - timedelta(days=SIGNAL_RETENTION_DEFAULT_DAYS + 1)
    (tmp_path / CAPTURE_LOG_NAME).write_text(_log_line(just_over) + "\n", encoding="utf-8")

    assert prune_capture_log(tmp_path, now=NOW) == 1


def test_prune_capture_log_is_atomic_and_leaves_no_temp_file(tmp_path):
    (tmp_path / CAPTURE_LOG_NAME).write_text(
        _log_line(NOW - timedelta(days=200)) + "\n" + _log_line(NOW) + "\n", encoding="utf-8"
    )

    prune_capture_log(tmp_path, retention_days=180, now=NOW)

    assert sorted(p.name for p in tmp_path.iterdir()) == [CAPTURE_LOG_NAME]


def test_describe_mentions_capture_only_when_on(tmp_path):
    assert not any(line.startswith("capture") for line in load_config(config_dir=tmp_path).describe())
    set_capture(tmp_path, level="deep", sample=10, now=NOW)
    assert "capture: deep, 10% of sessions" in load_config(config_dir=tmp_path).describe()

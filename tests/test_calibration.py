"""Tests for characters per token, measured on the corpus's own first calls
(src/claudeglass/calibration.py)."""

from __future__ import annotations

import pytest

from claudeglass import calibration
from claudeglass.calibration import FALLBACK, MAX_RATIO, MIN_CALLS, MIN_RATIO, Calibration, FirstCall, model_family


def _call(model="claude-sonnet-5", tool_chars=4_340, text_chars=3_000, prefix=1_000, own=1_000) -> FirstCall:
    return FirstCall(model=model, tool_chars=tool_chars, text_chars=text_chars, shared_prefix_tokens=prefix, own_tokens=own)


def test_a_family_with_ten_first_calls_gets_the_median_of_its_ratios():
    # Tool ratios 2.0, 2.5 ... 6.5 characters per token: the median of ten is 4.25.
    # Text is 500 characters more per 1,000 tokens: 2.5 ... 7.0, median 4.75.
    calls = [_call(tool_chars=2_000 + n * 500, text_chars=2_500 + n * 500) for n in range(MIN_CALLS)]
    found = Calibration.from_calls(calls)
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.25)
    assert found.text_chars_per_token("claude-sonnet-5") == pytest.approx(4.75)
    assert found.calibrated
    assert found.basis() == calibration.BASIS


def test_a_median_ignores_one_wild_call():
    calls = [_call() for _ in range(MIN_CALLS)] + [_call(tool_chars=4_340_000)]
    assert Calibration.from_calls(calls).tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)


def test_fewer_than_ten_calls_fall_back_to_four():
    found = Calibration.from_calls([_call() for _ in range(MIN_CALLS - 1)])
    assert found.tool_chars_per_token("claude-sonnet-5") == FALLBACK == 4.0
    assert found.text_chars_per_token("claude-sonnet-5") == 4.0
    assert not found.calibrated
    assert found.basis() == calibration.FALLBACK_BASIS
    assert found.rows() == []


def test_a_model_with_no_calls_uses_four_while_another_is_calibrated():
    found = Calibration.from_calls([_call("claude-haiku-4-5", tool_chars=5_810) for _ in range(MIN_CALLS)])
    assert found.tool_chars_per_token("claude-haiku-4-5") == pytest.approx(5.81)
    assert found.tool_chars_per_token("claude-opus-5-5") == 4.0


def test_a_cold_start_has_no_shared_prefix_to_measure_and_is_skipped():
    cold = [_call(prefix=0) for _ in range(MIN_CALLS)]
    found = Calibration.from_calls(cold)
    assert found.tool_chars_per_token("claude-sonnet-5") == 4.0
    assert found.text_chars_per_token("claude-sonnet-5") == 4.0
    assert found.default_family == "claude-sonnet-5"


def test_a_call_with_no_recorded_text_adds_to_the_tool_ratio_only():
    found = Calibration.from_calls([_call(text_chars=0, own=0) for _ in range(MIN_CALLS)])
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)
    assert found.text_chars_per_token("claude-sonnet-5") == 4.0


def test_a_call_with_no_tools_snapshot_adds_to_neither_ratio():
    # Ten calls with a snapshot: tools 4.34, text 3.0. Twenty with none: the
    # tool definitions they wrote are in their own tokens (3,000 characters
    # of text over 3,400 tokens is 0.88 a token), which is no text ratio.
    with_tools = [_call() for _ in range(MIN_CALLS)]
    without = [_call(tool_chars=0, text_chars=3_000, prefix=1_000, own=3_400) for _ in range(2 * MIN_CALLS)]
    found = Calibration.from_calls(with_tools + without)
    assert found.text_chars_per_token("claude-sonnet-5") == pytest.approx(3.0)
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)
    # Only the calls with a snapshot count toward the ten a family needs.
    thin = Calibration.from_calls([_call() for _ in range(MIN_CALLS - 1)] + without)
    assert thin.text_chars_per_token("claude-sonnet-5") == 4.0
    assert thin.tool_chars_per_token("claude-sonnet-5") == 4.0
    assert not thin.calibrated
    # A family with no snapshot at all keeps the fallback, and is still the most common one.
    none = Calibration.from_calls(without)
    assert none.text_chars_per_token("claude-sonnet-5") == 4.0 and none.default_family == "claude-sonnet-5"


def test_a_ratio_outside_the_plausible_band_is_dropped_before_the_median():
    # Ten good calls and fifteen whose characters and tokens do not describe
    # the same text. Without the band the median would be 0.5.
    good = [_call(tool_chars=4_340, text_chars=3_000) for _ in range(MIN_CALLS)]
    low = [_call(tool_chars=300, text_chars=500) for _ in range(15)]
    found = Calibration.from_calls(good + low)
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)
    assert found.text_chars_per_token("claude-sonnet-5") == pytest.approx(3.0)
    high = [_call(tool_chars=30_000, text_chars=30_000) for _ in range(15)]
    found = Calibration.from_calls(good + high)
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)
    assert found.text_chars_per_token("claude-sonnet-5") == pytest.approx(3.0)


def test_the_band_is_one_and_a_half_to_eight_characters_per_token_and_includes_its_ends():
    assert (MIN_RATIO, MAX_RATIO) == (1.5, 8.0)
    edges = [_call(tool_chars=1_500, text_chars=1_500) for _ in range(5)] + [
        _call(tool_chars=8_000, text_chars=8_000) for _ in range(5)
    ]
    found = Calibration.from_calls(edges)
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx((1.5 + 8.0) / 2)
    just_out = [_call(tool_chars=1_499, text_chars=1_499) for _ in range(5)] + [
        _call(tool_chars=8_001, text_chars=8_001) for _ in range(5)
    ]
    assert Calibration.from_calls(just_out).tool_chars_per_token("claude-sonnet-5") == 4.0


def test_fewer_than_ten_plausible_calls_fall_back_to_four():
    # Twelve calls, but only nine within the band: not enough to calibrate on.
    calls = [_call() for _ in range(MIN_CALLS - 1)] + [_call(tool_chars=50, text_chars=50) for _ in range(3)]
    found = Calibration.from_calls(calls)
    assert found.tool_chars_per_token("claude-sonnet-5") == 4.0
    assert found.text_chars_per_token("claude-sonnet-5") == 4.0
    assert found.basis() == calibration.FALLBACK_BASIS
    # The tenth plausible call tips it over.
    found = Calibration.from_calls(calls + [_call()])
    assert found.tool_chars_per_token("claude-sonnet-5") == pytest.approx(4.34)
    assert found.text_chars_per_token("claude-sonnet-5") == pytest.approx(3.0)


def test_the_model_family_drops_the_date_and_the_context_tag():
    assert model_family("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert model_family("Claude-Sonnet-5[1m]") == "claude-sonnet-5"
    assert model_family("claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert model_family("<synthetic>") == ""
    assert model_family(None) == ""
    # Sonnet 5 and Sonnet 5.5 do not tokenize alike: they are not one family.
    assert model_family("claude-sonnet-5") != model_family("claude-sonnet-5-5")


def test_a_caller_that_names_no_model_gets_the_most_common_family():
    calls = [_call("claude-haiku-4-5", tool_chars=5_810) for _ in range(MIN_CALLS + 2)] + [
        _call("claude-sonnet-5") for _ in range(MIN_CALLS)
    ]
    found = Calibration.from_calls(calls)
    assert found.default_family == "claude-haiku-4-5"
    assert found.tool_chars_per_token() == pytest.approx(5.81)
    assert found.tool_chars_per_token("<synthetic>") == pytest.approx(5.81)
    assert found.tool_tokens(5_810) == pytest.approx(1_000)
    assert found.text_tokens(3_000, "claude-sonnet-5") == pytest.approx(1_000)


def test_the_default_calibration_is_chars_over_four():
    assert calibration.DEFAULT.tool_tokens(400) == 100
    assert Calibration().text_tokens(400, "claude-sonnet-5") == 100


def test_only_two_numbers_per_family_are_kept_and_they_round_trip():
    found = Calibration.from_calls([_call() for _ in range(MIN_CALLS)])
    data = found.to_dict()
    assert set(data) == {"tool", "text", "default_family"}
    assert all(isinstance(v, float) for v in data["tool"].values())
    again = Calibration.from_dict(data)
    assert again.tool == found.tool and again.text == found.text
    assert again.default_family == found.default_family
    assert Calibration.from_dict(None).tool == {}
    assert Calibration.from_dict({"tool": {"m": -1, "n": "x", "o": True, "p": 4.5}}).tool == {"p": 4.5}


def test_the_rows_list_the_most_common_family_first():
    calls = [_call("claude-sonnet-5") for _ in range(MIN_CALLS)] + [
        _call("claude-haiku-4-5", tool_chars=5_810) for _ in range(MIN_CALLS + 3)
    ]
    rows = Calibration.from_calls(calls).rows()
    assert [row[0] for row in rows] == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert rows[0][1] == pytest.approx(5.81)

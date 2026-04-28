"""Tests for calculate_bearish_signals."""
import pandas as pd

from indicators.technical import calculate_bearish_signals


def test_insufficient_data_returns_minus_99():
    df = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 100, 100]})
    result = calculate_bearish_signals(df, "TEST")
    assert result.score == -99
    assert result.signals == ["INSUFFICIENT_DATA"]


def test_trending_down_produces_positive_bearish_score(trending_down_df):
    result = calculate_bearish_signals(trending_down_df, "TEST")
    assert result.score > 0, f"expected positive bearish score, got {result.score}"
    assert result.symbol == "TEST"


def test_trending_up_produces_low_bearish_score(trending_up_df):
    result = calculate_bearish_signals(trending_up_df, "TEST")
    assert result.score <= 0, f"expected non-positive bearish score, got {result.score}"


def test_flat_data_produces_low_bearish_score(flat_df):
    result = calculate_bearish_signals(flat_df, "TEST")
    assert -2 <= result.score <= 2

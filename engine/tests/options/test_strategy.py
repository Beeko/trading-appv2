"""Tests for OptionsSignal generation."""
import pandas as pd

from options.strategy import OptionsSignal, score_options_signal


def test_insufficient_data_returns_neutral():
    df = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 100, 100]})
    sig = score_options_signal("TEST", df, min_score=3)
    assert sig.direction == "neutral"
    assert sig.score == 0


def test_bullish_trend_classified_bullish(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=3)
    assert sig.direction == "bullish"
    assert sig.score >= 3
    assert sig.symbol == "TEST"


def test_bearish_trend_classified_bearish(trending_down_df):
    sig = score_options_signal("TEST", trending_down_df, min_score=3)
    assert sig.direction == "bearish"
    assert sig.score >= 3


def test_flat_data_classified_neutral(flat_df):
    sig = score_options_signal("TEST", flat_df, min_score=3)
    assert sig.direction == "neutral"


def test_higher_threshold_demotes_borderline_to_neutral(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=99)
    assert sig.direction == "neutral"


def test_signal_carries_indicator_metrics(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=3)
    assert sig.price > 0
    assert sig.rsi > 0
    assert sig.volume_ratio > 0
    assert isinstance(sig.signals, list)

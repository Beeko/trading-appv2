"""Options signal generation. Wraps the bullish/bearish indicator scorers and
classifies into a directional signal the contract selector can act on."""
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from indicators.technical import calculate_bearish_signals, calculate_signals


Direction = Literal["bullish", "bearish", "neutral"]


@dataclass
class OptionsSignal:
    symbol: str
    direction: Direction
    score: int
    bullish_score: int
    bearish_score: int
    signals: list[str]
    rsi: float = 0.0
    volume_ratio: float = 0.0
    price: float = 0.0


def score_options_signal(
    symbol: str, df: pd.DataFrame, min_score: int = 3
) -> OptionsSignal:
    """Compute bullish + bearish scores; classify the dominant direction.

    Tied scores → neutral. Both must clear `min_score` for direction to fire.
    """
    bull = calculate_signals(df, symbol)
    bear = calculate_bearish_signals(df, symbol)

    if bull.score == -99 or bear.score == -99:
        return OptionsSignal(
            symbol=symbol, direction="neutral", score=0,
            bullish_score=0, bearish_score=0, signals=["INSUFFICIENT_DATA"],
        )

    bull_qualifies = bull.score >= min_score
    bear_qualifies = bear.score >= min_score

    if bull_qualifies and not bear_qualifies:
        direction: Direction = "bullish"
        winner = bull
    elif bear_qualifies and not bull_qualifies:
        direction = "bearish"
        winner = bear
    elif bull_qualifies and bear_qualifies:
        if bull.score > bear.score:
            direction, winner = "bullish", bull
        elif bear.score > bull.score:
            direction, winner = "bearish", bear
        else:
            return OptionsSignal(
                symbol=symbol, direction="neutral", score=0,
                bullish_score=bull.score, bearish_score=bear.score,
                signals=["TIED_SCORES"], rsi=bull.rsi,
                volume_ratio=bull.volume_ratio, price=bull.price,
            )
    else:
        return OptionsSignal(
            symbol=symbol, direction="neutral", score=0,
            bullish_score=bull.score, bearish_score=bear.score,
            signals=[], rsi=bull.rsi,
            volume_ratio=bull.volume_ratio, price=bull.price,
        )

    return OptionsSignal(
        symbol=symbol,
        direction=direction,
        score=winner.score,
        bullish_score=bull.score,
        bearish_score=bear.score,
        signals=winner.signals,
        rsi=winner.rsi,
        volume_ratio=winner.volume_ratio,
        price=winner.price,
    )

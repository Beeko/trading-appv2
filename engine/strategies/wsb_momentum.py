"""WSB Momentum strategy: technical confluence on a watchlist of momentum names.

Scoring is delegated to indicators.technical.calculate_signals — this strategy
just provides the scoring entrypoint and (eventually) the dynamic WSB watchlist.
"""
import pandas as pd

from indicators.technical import SignalScore, calculate_signals
from strategies.base import Strategy


class WSBMomentumStrategy(Strategy):
    name = "wsb_momentum"

    def score(self, symbol: str, df: pd.DataFrame) -> SignalScore:
        return calculate_signals(df, symbol=symbol)

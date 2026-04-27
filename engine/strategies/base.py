from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from indicators.technical import SignalScore


class Strategy(ABC):
    """Base strategy interface. Strategies score symbols and the engine decides
    which scores to act on based on the configured min_score_to_trade."""

    name: str = "base"

    @abstractmethod
    def score(self, symbol: str, df: pd.DataFrame) -> SignalScore:
        ...

    def watchlist(self, default: list[str]) -> list[str]:
        """Override to provide a dynamic watchlist (e.g. WSB scanner)."""
        return default

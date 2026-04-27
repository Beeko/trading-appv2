"""Dynamic watchlist via Alpaca's market screener endpoints.

Results are cached for 5 minutes so the screener doesn't hammer the data API
on every engine tick.
"""
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from data.alpaca_client import AlpacaClient

_CACHE_TTL = 300  # seconds


class MarketScreener:
    """Builds a watchlist from Alpaca most-actives and optional top-gainers."""

    def __init__(self, client: "AlpacaClient"):
        self._client = client
        self._cache: list[str] = []
        self._cache_time: float = 0.0

    async def get_symbols(
        self,
        top_n: int = 50,
        include_gainers: bool = True,
        min_price: float = 5.0,
        max_price: float = 500.0,
    ) -> list[str]:
        now = time.monotonic()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            logger.debug(f"Screener cache hit ({len(self._cache)} symbols)")
            return self._cache

        candidates: list[str] = []

        try:
            actives = await self._client.get_most_actives(top=top_n)
            candidates.extend(actives)
        except Exception as e:
            logger.warning(f"Screener most-actives failed: {e}")

        if include_gainers:
            try:
                gainers = await self._client.get_top_gainers(top=max(top_n // 2, 10))
                candidates.extend(gainers)
            except Exception as e:
                logger.warning(f"Screener gainers failed: {e}")

        if not candidates:
            logger.warning("Screener: no candidates from Alpaca")
            return []

        seen: set[str] = set()
        unique: list[str] = []
        for s in candidates:
            if s not in seen:
                seen.add(s)
                unique.append(s)

        filtered = await self._client.filter_symbols_by_price(
            unique, min_price=min_price, max_price=max_price
        )
        result = filtered[:top_n]
        logger.info(
            f"Screener: {len(candidates)} raw → {len(unique)} unique → "
            f"{len(filtered)} price-ok → {len(result)} final"
        )
        self._cache = result
        self._cache_time = now
        return result

    def invalidate_cache(self) -> None:
        self._cache = []
        self._cache_time = 0.0

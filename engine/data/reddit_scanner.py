"""WSB ticker scanner. Optional v1 module — disabled by default in config.yaml.

Two paths:
  • PRAW (authenticated) — reliable, requires Reddit app credentials in .env
  • Public JSON endpoint — no auth, but Reddit aggressively rate-limits

If credentials are absent or scanning fails, the trading engine falls back to the
hardcoded watchlist in config.yaml.
"""
import asyncio
import re
from collections import Counter
from typing import Optional

import aiohttp
from loguru import logger

# Common false-positive uppercase words that are also valid tickers; we also
# intersect against Alpaca's tradable-asset list before publishing the watchlist.
_STOPWORDS = {
    "DD", "YOLO", "FD", "FDS", "TLDR", "EOD", "ATH", "ATL", "OP", "PR",
    "CEO", "CFO", "CTO", "USA", "US", "UK", "EU", "EOY", "QOQ", "YOY",
    "AI", "ML", "API", "APY", "APR", "CPI", "EPS", "ETF", "FED", "FOMO",
    "GDP", "IPO", "ITM", "OTM", "PE", "PEG", "ROE", "ROI", "SP", "TA",
    "WSB", "ELI5", "IRA", "ROTH", "FAANG", "MOON", "BTFD", "HODL",
    "I", "A", "U", "AM", "PM", "AN", "AS", "AT", "BE", "BY", "DO", "GO",
    "HE", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OH", "OK",
    "ON", "OR", "SO", "TO", "UP", "WE", "ALL", "AND", "ANY", "ARE",
    "BUT", "CAN", "FOR", "GET", "HAS", "HAD", "HER", "HIM", "HIS", "HOW",
    "ITS", "MAY", "NEW", "NOT", "NOW", "OUR", "OUT", "SHE", "THE", "TWO",
    "WAS", "WHO", "WHY", "YOU", "OPEN", "DEAL", "GAIN", "LOSS", "BUY",
    "SELL", "HOLD", "BIG", "SHIT", "FUCK", "FUCKING", "HUGE", "TAKE",
    "MAKE", "GIVE", "LIKE", "LOVE", "JUST", "WHAT", "WHEN", "WITH",
    "POST", "MORE", "BEEN", "GOOD", "TIME", "WORK", "FROM", "SOME",
    "ALSO", "ABLE", "ONLY", "EVEN", "BACK", "MUCH", "WELL", "WANT",
    "EVER", "OVER", "SAID", "SHIT", "DAMN", "FOMC", "SEC", "FDA", "IRS",
    "FAQ", "WTF", "RIP",
}

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b")


def extract_tickers(text: str, valid_symbols: Optional[set[str]] = None) -> list[str]:
    """Extract candidate tickers. If valid_symbols is provided, results are
    intersected against it to filter out non-tradable false positives."""
    if not text:
        return []
    candidates: list[str] = []
    for m in _TICKER_RE.finditer(text):
        sym = m.group(1) or m.group(2)
        if not sym or sym in _STOPWORDS:
            continue
        # Single-letter symbols (e.g. F, T) are valid but only if cashtagged
        if len(sym) == 1 and not m.group(1):
            continue
        candidates.append(sym)

    if valid_symbols is not None:
        candidates = [c for c in candidates if c in valid_symbols]
    return candidates


class RedditScanner:
    """Two-mode scraper. Use PRAW if creds are set, else public JSON."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        user_agent: str = "TradingBot/1.0",
    ):
        self.user_agent = user_agent
        self._praw = None
        if client_id and client_secret:
            try:
                import praw  # type: ignore
                self._praw = praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_agent=user_agent,
                )
                logger.info("Reddit scanner: using PRAW (authenticated)")
            except ImportError:
                logger.warning("praw not installed; falling back to public JSON")

    async def get_trending_tickers(
        self,
        subreddit: str = "wallstreetbets",
        limit: int = 100,
        valid_symbols: Optional[set[str]] = None,
    ) -> Counter:
        """Returns Counter of {ticker: mention_count} from hot posts."""
        texts: list[str] = []
        if self._praw is not None:
            try:
                texts = await asyncio.to_thread(self._fetch_via_praw, subreddit, limit)
            except Exception as e:
                logger.warning(f"PRAW fetch failed, falling back to JSON: {e}")
        if not texts:
            texts = await self._fetch_via_json(subreddit, limit)

        counter: Counter = Counter()
        for txt in texts:
            counter.update(extract_tickers(txt, valid_symbols))
        return counter

    def _fetch_via_praw(self, subreddit: str, limit: int) -> list[str]:
        texts: list[str] = []
        for post in self._praw.subreddit(subreddit).hot(limit=limit):
            texts.append(post.title or "")
            if hasattr(post, "selftext") and post.selftext:
                texts.append(post.selftext)
        return texts

    async def _fetch_via_json(self, subreddit: str, limit: int) -> list[str]:
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, timeout=10) as resp:
                    if resp.status == 429:
                        logger.warning("Reddit rate-limited (429); skipping WSB scan")
                        return []
                    if resp.status != 200:
                        logger.warning(f"Reddit returned {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"Reddit JSON fetch failed: {e}")
            return []

        texts: list[str] = []
        children = data.get("data", {}).get("children", [])
        for child in children:
            pdata = child.get("data", {})
            texts.append(pdata.get("title") or "")
            texts.append(pdata.get("selftext") or "")
        return texts

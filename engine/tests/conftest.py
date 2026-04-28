"""Shared pytest fixtures for engine tests."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def trending_up_df() -> pd.DataFrame:
    """40 bars of cleanly trending-up OHLCV data — should produce a bullish score."""
    n = 40
    base = np.linspace(100, 130, n)
    rng = np.random.default_rng(seed=42)
    noise = rng.normal(0, 0.3, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    volume = rng.integers(800_000, 1_500_000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def trending_down_df() -> pd.DataFrame:
    """40 bars of cleanly trending-down OHLCV data — should produce a bearish score."""
    n = 40
    base = np.linspace(130, 100, n)
    rng = np.random.default_rng(seed=43)
    noise = rng.normal(0, 0.3, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close + 0.1
    volume = rng.integers(800_000, 1_500_000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def flat_df() -> pd.DataFrame:
    """40 bars of flat data — should produce a neutral score."""
    n = 40
    rng = np.random.default_rng(seed=44)
    close = 100 + rng.normal(0, 0.2, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 1_000_000),
        },
        index=idx,
    )


@pytest.fixture
def mock_alpaca():
    """AsyncMock AlpacaClient with the methods used by the options engine."""
    m = MagicMock()
    m.get_account = AsyncMock(return_value={
        "equity": 100_000.0, "cash": 50_000.0, "buying_power": 50_000.0,
        "trading_blocked": False, "account_blocked": False,
        "daytrade_count": 0, "pattern_day_trader": False,
    })
    m.get_positions = AsyncMock(return_value=[])
    m.is_market_open = AsyncMock(return_value=True)
    m.get_clock = AsyncMock(return_value={"is_open": True})
    m.get_bars = AsyncMock(return_value=None)
    m.get_option_chain = AsyncMock(return_value=[])
    m.get_option_snapshots = AsyncMock(return_value={})
    m.get_option_snapshot = AsyncMock(return_value=None)
    m.submit_option_limit_order = AsyncMock(return_value={
        "id": "broker-id-1", "client_order_id": "test", "status": "accepted", "qty": 1,
    })
    m.cancel_option_order = AsyncMock()
    return m


@pytest.fixture
def mock_repo():
    """AsyncMock Repository — only the methods the options engine touches."""
    m = MagicMock()
    m.log_event = AsyncMock()
    m.insert_signal = AsyncMock()
    m.insert_option_trade_pending = AsyncMock(return_value=1)
    m.update_option_trade_after_submit = AsyncMock()
    m.update_option_trade_with_entry_data = AsyncMock()
    m.update_option_trade_exit = AsyncMock()
    m.list_open_option_trades = AsyncMock(return_value=[])
    return m


@pytest.fixture
def mock_risk():
    """MagicMock RiskManager — synchronous methods for risk gates."""
    m = MagicMock()
    m.kill_switch_active = MagicMock(return_value=False)
    m.daily_loss_breached = MagicMock(return_value=False)
    m.daily_profit_goal_reached = MagicMock(return_value=False)
    m.trading_mode = MagicMock(return_value="paper")
    m.initialize_daily_baseline = AsyncMock()
    m.daily_start_equity = 100_000.0
    return m


@pytest.fixture
def sample_snapshot():
    """A typical valid option snapshot dict (post-AlpacaClient normalization)."""
    return {
        "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
        "implied_volatility": 0.35,
        "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
        "volume": 250, "open_interest": 1500,
        "last_price": 2.55,
    }


@pytest.fixture
def sample_chain_contract():
    """A typical chain contract dict (post-AlpacaClient normalization)."""
    today = date.today()
    return {
        "symbol": "AAPL260619C00200000",
        "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0,
        "close_price": 2.50,
        "open_interest": 1500,
        "tradable": True,
    }

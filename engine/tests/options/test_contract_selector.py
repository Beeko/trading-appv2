"""Tests for ContractSelector pipeline."""
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from options.contract_selector import ContractSelector, SelectorConfig


def _contract(symbol: str, ctype: str, dte: int, strike: float) -> dict:
    return {
        "symbol": symbol,
        "underlying_symbol": "AAPL",
        "contract_type": ctype,
        "expiration_date": (date.today() + timedelta(days=dte)).isoformat(),
        "strike_price": strike,
        "open_interest": 1000,
        "tradable": True,
    }


def _snap(*, delta=0.40, bid=2.50, ask=2.60, vol=250, oi=1500, iv=0.35):
    return {
        "delta": delta, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
        "implied_volatility": iv,
        "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
        "spread_pct": (ask - bid) / ((bid + ask) / 2) if bid + ask > 0 else None,
        "volume": vol, "open_interest": oi, "last_price": (bid + ask) / 2,
    }


@pytest.fixture
def cfg():
    return SelectorConfig(
        target_delta=0.40, delta_tolerance=0.05,
        min_dte=28, max_dte=45,
        max_spread_pct=0.20, min_volume=10, min_open_interest=100,
        dte_floor=21,
    )


def _selector(client, cfg):
    return ContractSelector(client, cfg)


async def test_select_returns_none_when_chain_empty(mock_alpaca, cfg):
    mock_alpaca.get_option_chain = AsyncMock(return_value=[])
    sel = _selector(mock_alpaca, cfg)
    result = await sel.select("AAPL", "bullish")
    assert result is None


async def test_select_filters_dte_window(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=10, strike=200),
        _contract("B", "call", dte=30, strike=200),
        _contract("C", "call", dte=60, strike=200),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={"B": _snap(delta=0.40)})

    sel = _selector(mock_alpaca, cfg)
    result = await sel.select("AAPL", "bullish")

    assert result is not None
    assert result.contract_symbol == "B"
    mock_alpaca.get_option_snapshots.assert_called_once_with(["B"])


async def test_select_rejects_wide_spread(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(bid=2.0, ask=3.0, delta=0.40)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_rejects_low_volume(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(vol=5, delta=0.40)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_rejects_low_open_interest(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(oi=50, delta=0.40)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_filters_delta_outside_tolerance(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.20)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_picks_delta_closest_to_target(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=30, strike=195),
        _contract("B", "call", dte=30, strike=200),
        _contract("C", "call", dte=30, strike=205),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.45),
        "B": _snap(delta=0.41),
        "C": _snap(delta=0.36),
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is not None
    assert result.contract_symbol == "B"
    assert result.delta == 0.41


async def test_select_for_bearish_uses_negative_delta_target(mock_alpaca, cfg):
    chain = [
        _contract("A", "put", dte=30, strike=200),
        _contract("B", "put", dte=30, strike=195),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=-0.41),
        "B": _snap(delta=-0.30),
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bearish")
    assert result is not None
    assert result.contract_symbol == "A"
    assert result.delta == -0.41


async def test_select_breaks_ties_with_higher_open_interest(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=30, strike=200),
        _contract("B", "call", dte=30, strike=205),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.40, oi=500),
        "B": _snap(delta=0.40, oi=2500),
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is not None
    assert result.contract_symbol == "B"


async def test_select_rejects_dte_at_or_below_floor(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=21, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={"A": _snap(delta=0.40)})
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None

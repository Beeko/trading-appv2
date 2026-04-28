"""Tests for PositionMonitor exit-trigger evaluation and async loop."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from options.position_monitor import (
    ExitDecision, MonitorConfig, PositionMonitor, evaluate_exit,
)


@pytest.fixture
def cfg():
    return MonitorConfig(
        profit_target_pct=0.50,
        stop_loss_pct=0.50,
        dte_floor=21,
    )


@pytest.fixture
def cfg10():
    return MonitorConfig(
        profit_target_pct=0.50, stop_loss_pct=0.50, dte_floor=21,
        fill_timeout_seconds=120, exit_retry_max=3, exit_retry_price_step_pct=0.05,
    )


def test_no_trigger_when_at_break_even(cfg):
    decision = evaluate_exit(entry_mid=2.50, current_mid=2.50, dte=30, cfg=cfg)
    assert decision is None


def test_profit_target_fires_at_50pct_gain(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=3.00, dte=30, cfg=cfg)
    assert decision is not None
    assert decision.reason == "profit_target"


def test_stop_loss_fires_at_50pct_loss(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=1.00, dte=30, cfg=cfg)
    assert decision is not None
    assert decision.reason == "stop_loss"


def test_dte_floor_fires_below_threshold(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=2.05, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_dte_floor_takes_priority_over_profit_target(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=3.50, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_dte_floor_takes_priority_over_stop_loss(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=0.50, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_zero_entry_mid_returns_none_safely(cfg):
    decision = evaluate_exit(entry_mid=0.0, current_mid=1.0, dte=30, cfg=cfg)
    assert decision is None


def test_at_dte_floor_exactly_does_not_fire(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=2.05, dte=21, cfg=cfg)
    assert decision is None


def _open_trade(*, client_order_id="opt_AAPL_x", contract_symbol="AAPL260619C00200000",
                entry_mid=2.50, dte_at_entry=30, qty=1):
    return {
        "client_order_id": client_order_id,
        "broker_order_id": "broker-1",
        "contract_symbol": contract_symbol,
        "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (date.today() + timedelta(days=dte_at_entry)).isoformat(),
        "strike_price": 200.0,
        "side": "buy",
        "qty": qty,
        "status": "filled",
        "entry_mid": entry_mid,
        "dte_at_entry": dte_at_entry,
    }


async def test_tick_no_open_positions_short_circuits(mock_alpaca, mock_repo, cfg10):
    mock_repo.list_open_option_trades = AsyncMock(return_value=[])
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()
    mock_alpaca.get_option_snapshots.assert_not_called()


async def test_tick_fires_profit_target_exit(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 3.10, "ask": 3.20, "mid": 3.15,
            "delta": 0.50, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.03,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    mock_alpaca.submit_option_limit_order.assert_called_once()
    kwargs = mock_alpaca.submit_option_limit_order.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["qty"] == 1
    assert kwargs["limit_price"] == 3.15

    mock_repo.update_option_trade_exit.assert_called_once()
    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "profit_target"


async def test_tick_fires_dte_floor_exit_with_priority(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00, dte_at_entry=22)
    trade["expiration_date"] = (date.today() + timedelta(days=20)).isoformat()
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 3.50, "ask": 3.60, "mid": 3.55,
            "delta": 0.50, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.03,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "dte_floor"


async def test_tick_skips_position_when_no_snapshot(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={})
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()
    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_tick_no_trigger_no_action(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00, dte_at_entry=30)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 2.00, "ask": 2.10, "mid": 2.05,
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.05,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()
    mock_alpaca.submit_option_limit_order.assert_not_called()
    mock_repo.update_option_trade_exit.assert_not_called()


async def test_startup_sweep_force_closes_below_dte_floor(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00)
    trade["expiration_date"] = (date.today() + timedelta(days=10)).isoformat()
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 1.00, "ask": 1.10, "mid": 1.05,
            "delta": 0.20, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.10,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.startup_sweep()

    mock_alpaca.submit_option_limit_order.assert_called_once()
    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "dte_floor"

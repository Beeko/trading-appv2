"""Tests for OptionsEngine lifecycle and tick orchestration."""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from options.engine import OptionsEngine


def _options_config_dict():
    return {
        "enabled": True,
        "scan_interval_seconds": 300,
        "monitor_interval_seconds": 60,
        "bar_timeframe": "5Min",
        "lookback_bars": 78,
        "min_score_to_trade": 3,
        "max_option_positions": 5,
        "max_position_pct": 0.02,
        "target_delta": 0.40,
        "delta_tolerance": 0.05,
        "min_dte": 28,
        "max_dte": 45,
        "profit_target_pct": 0.50,
        "stop_loss_pct": 0.50,
        "dte_floor": 21,
        "liquidity": {
            "max_spread_pct": 0.20, "min_volume": 10, "min_open_interest": 100,
        },
        "limit_order": {
            "fill_timeout_seconds": 120, "exit_retry_max": 3,
            "exit_retry_price_step_pct": 0.05,
        },
    }


@pytest.fixture
def cfg_obj():
    cfg = MagicMock()
    cfg.options = _options_config_dict()
    cfg.trading = {"watchlist": ["AAPL", "TSLA"]}
    cfg.screener = {"enabled": False}
    cfg.wsb_scanner = {"enabled": False}
    cfg.validate_options_config = MagicMock(return_value=[])
    return cfg


@pytest.fixture
def settings():
    s = MagicMock()
    s.reddit_client_id = None
    s.reddit_client_secret = None
    return s


def _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    return OptionsEngine(
        settings=settings, config=cfg_obj, client=mock_alpaca,
        risk=mock_risk, repo=mock_repo,
    )


def test_engine_starts_unpaused(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    assert eng.paused is False
    assert eng.running is False


def test_pause_and_resume(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    eng.pause()
    assert eng.paused is True
    eng.resume()
    assert eng.paused is False


async def test_tick_skips_when_paused(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    eng.pause()
    await eng._tick()
    mock_alpaca.is_market_open.assert_not_called()


async def test_tick_skips_when_kill_switch_active(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_risk.kill_switch_active.return_value = True
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    mock_alpaca.is_market_open.assert_not_called()


async def test_tick_skips_when_market_closed(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_alpaca.is_market_open = AsyncMock(return_value=False)
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    mock_alpaca.get_account.assert_not_called()


async def test_tick_pauses_engine_on_daily_loss_breach(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_risk.daily_loss_breached.return_value = True
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    assert eng.paused is True


async def test_scan_executes_buy_call_on_bullish_signal(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)

    today = date.today()
    chain = [{
        "symbol": "AAPL_C", "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0, "open_interest": 1500, "tradable": True,
    }]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "AAPL_C": {
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35,
            "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
            "volume": 250, "open_interest": 1500, "last_price": 2.55,
        }
    })
    mock_alpaca.get_positions = AsyncMock(return_value=[])
    mock_repo.list_open_option_trades = AsyncMock(return_value=[])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_repo.insert_option_trade_pending.assert_called_once()
    mock_alpaca.submit_option_limit_order.assert_called_once()
    submit_kwargs = mock_alpaca.submit_option_limit_order.call_args.kwargs
    assert submit_kwargs["side"] == "buy"
    assert submit_kwargs["limit_price"] == 2.55
    assert submit_kwargs["contract_symbol"] == "AAPL_C"
    mock_repo.update_option_trade_after_submit.assert_called()
    mock_repo.update_option_trade_with_entry_data.assert_called()


async def test_scan_skips_underlying_when_already_held(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[{
        "underlying_symbol": "AAPL", "contract_symbol": "AAPL_OLD",
        "client_order_id": "old", "side": "buy", "qty": 1, "status": "filled",
        "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
        "entry_mid": 2.0,
    }])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)
    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_when_max_positions_reached(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    opts = _options_config_dict()
    opts["max_option_positions"] = 1
    cfg_obj.options = opts
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[{
        "underlying_symbol": "TSLA", "contract_symbol": "TSLA_OLD",
        "client_order_id": "old", "side": "buy", "qty": 1, "status": "filled",
        "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
        "entry_mid": 2.0,
    }])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)
    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_when_qty_below_one(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_account = AsyncMock(return_value={
        "equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0,
        "trading_blocked": False, "account_blocked": False,
    })
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[])

    today = date.today()
    chain = [{
        "symbol": "AAPL_C", "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0, "open_interest": 1500, "tradable": True,
    }]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "AAPL_C": {
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35,
            "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
            "volume": 250, "open_interest": 1500, "last_price": 2.55,
        }
    })

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)
    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_neutral_signals(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, flat_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=flat_df)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.get_option_chain.assert_not_called()
    mock_alpaca.submit_option_limit_order.assert_not_called()

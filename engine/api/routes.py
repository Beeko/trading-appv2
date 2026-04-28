"""FastAPI routes. All shared resources (alpaca client, engine, repo, config)
are accessed via request.app.state."""
import math
import re
import time as _time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from indicators.technical import (
    bollinger_bands,
    calculate_signals,
    ema,
    macd as _macd,
    rsi as _rsi,
)

# Module-level TTL cache keyed by "SYMBOL:timeframe:limit"
_indicator_cache: dict[str, tuple[float, dict]] = {}
_INDICATOR_TTL = 60  # seconds


def _series_to_list(series) -> list:
    out = []
    for v in series:
        try:
            f = float(v)
            out.append(None if math.isnan(f) else round(f, 4))
        except (TypeError, ValueError):
            out.append(None)
    return out

router = APIRouter()


# ── health ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "engine_running": request.app.state.engine.running,
        "engine_paused": request.app.state.engine.paused,
        "kill_switch": request.app.state.risk.kill_switch_active(),
        "trading_mode": request.app.state.risk.trading_mode(),
    }


# ── account / market ─────────────────────────────────────────────────────────

@router.get("/account")
async def get_account(request: Request):
    try:
        return await request.app.state.alpaca.get_account()
    except Exception as e:
        raise HTTPException(500, f"alpaca account: {e}")


@router.get("/positions")
async def get_positions(request: Request):
    return await request.app.state.alpaca.get_positions()


@router.get("/clock")
async def get_clock(request: Request):
    return await request.app.state.alpaca.get_clock()


@router.get("/orders")
async def get_orders(request: Request, status: str = "all", limit: int = 50):
    return await request.app.state.alpaca.get_orders(status=status, limit=limit)


# ── trades / signals / events ────────────────────────────────────────────────

@router.get("/trades")
async def list_trades(request: Request, limit: int = 100):
    return await request.app.state.repo.list_recent_trades(limit=limit)


@router.get("/signals")
async def list_signals(request: Request, limit: int = 50):
    return await request.app.state.repo.list_recent_signals(limit=limit)


@router.get("/signals/current")
async def current_signals(request: Request):
    return request.app.state.engine.snapshot_signals()


@router.get("/events")
async def list_events(request: Request, limit: int = 50):
    return await request.app.state.repo.list_recent_events(limit=limit)


# ── engine control ───────────────────────────────────────────────────────────

@router.get("/engine/status")
async def engine_status(request: Request):
    risk = request.app.state.risk
    eng = request.app.state.engine
    return {
        "running": eng.running,
        "paused": eng.paused,
        "kill_switch": risk.kill_switch_active(),
        "trading_mode": risk.trading_mode(),
        "trading_style": risk.trading_style(),
        "live_trading_allowed": risk.live_trading_allowed(),
        "daily_start_equity": risk.daily_start_equity,
        "daily_profit_goal": risk.daily_profit_goal(),
    }


@router.post("/engine/pause")
async def engine_pause(request: Request):
    request.app.state.engine.pause()
    await request.app.state.repo.log_event("engine_paused", "via API")
    return {"ok": True, "paused": True}


@router.post("/engine/resume")
async def engine_resume(request: Request):
    request.app.state.engine.resume()
    await request.app.state.repo.log_event("engine_resumed", "via API")
    return {"ok": True, "paused": False}


# ── kill switch ──────────────────────────────────────────────────────────────

@router.post("/kill")
async def activate_kill(request: Request):
    request.app.state.risk.activate_kill_switch()
    await request.app.state.repo.log_event("kill_switch_activated", "via API")
    return {"ok": True, "kill_switch": True}


@router.post("/resume")
async def deactivate_kill(request: Request):
    request.app.state.risk.deactivate_kill_switch()
    await request.app.state.repo.log_event("kill_switch_cleared", "via API")
    return {"ok": True, "kill_switch": False}


# ── manual trade ─────────────────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    symbol: str
    side: str  # "buy" | "sell"
    amount: float = 0  # dollar amount; ignored on sell (closes whole position)


@router.post("/trade/manual")
async def manual_trade(req: ManualTradeRequest, request: Request):
    result = await request.app.state.engine.submit_manual_trade(
        symbol=req.symbol, side=req.side, dollar_amount=req.amount,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "trade failed"))
    return result


# ── liquidate (deliberately requires explicit confirm parameter) ─────────────

class LiquidateRequest(BaseModel):
    confirm: str  # must equal "LIQUIDATE_ALL"


@router.post("/positions/liquidate-all")
async def liquidate_all(req: LiquidateRequest, request: Request):
    if req.confirm != "LIQUIDATE_ALL":
        raise HTTPException(400, "confirm must equal 'LIQUIDATE_ALL'")
    await request.app.state.alpaca.close_all_positions()
    await request.app.state.repo.log_event("liquidate_all", "via API")
    return {"ok": True}


# ── indicator charts ──────────────────────────────────────────────────────────

@router.get("/indicators/{symbol}")
async def get_indicators(
    symbol: str,
    request: Request,
    timeframe: str = "1Day",
    limit: int = 100,
):
    symbol = symbol.upper().strip()
    cache_key = f"{symbol}:{timeframe}:{limit}"
    now = _time.monotonic()
    if cache_key in _indicator_cache:
        cached_at, cached_data = _indicator_cache[cache_key]
        if now - cached_at < _INDICATOR_TTL:
            return cached_data

    df = await request.app.state.alpaca.get_bars(
        symbol, timeframe=timeframe, limit=limit
    )
    if df is None or df.empty:
        raise HTTPException(404, f"No bar data for {symbol}")

    close = df["close"].astype(float)
    macd_line, sig_line, histogram = _macd(close)
    rsi_vals = _rsi(close)
    bb_upper, bb_mid, bb_lower = bollinger_bands(close)
    ema9 = ema(close, 9)
    ema21 = ema(close, 21)

    signal = calculate_signals(df, symbol)
    timestamps = [ts.isoformat() for ts in df.index]

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamps": timestamps,
        "ohlcv": {
            "open": _series_to_list(df["open"].astype(float)),
            "high": _series_to_list(df["high"].astype(float)),
            "low": _series_to_list(df["low"].astype(float)),
            "close": _series_to_list(close),
            "volume": [int(v) for v in df["volume"]],
        },
        "indicators": {
            "ema9": _series_to_list(ema9),
            "ema21": _series_to_list(ema21),
            "bb_upper": _series_to_list(bb_upper),
            "bb_mid": _series_to_list(bb_mid),
            "bb_lower": _series_to_list(bb_lower),
            "macd_line": _series_to_list(macd_line),
            "macd_signal": _series_to_list(sig_line),
            "macd_hist": _series_to_list(histogram),
            "rsi": _series_to_list(rsi_vals),
        },
        "signal": {
            "score": signal.score,
            "signals": signal.signals,
            "rsi": round(signal.rsi, 2),
            "volume_ratio": round(signal.volume_ratio, 2),
            "price": round(signal.price, 4),
        },
    }
    _indicator_cache[cache_key] = (now, result)
    return result


# ── screener ─────────────────────────────────────────────────────────────────

@router.get("/screener/symbols")
async def screener_symbols(request: Request):
    cfg = request.app.state.config.screener
    if not cfg.get("enabled", False):
        return {"enabled": False, "symbols": []}
    try:
        symbols = await request.app.state.engine._screener.get_symbols(
            top_n=int(cfg.get("top_n", 50)),
            include_gainers=bool(cfg.get("include_gainers", True)),
            min_price=float(cfg.get("min_price", 5.0)),
            max_price=float(cfg.get("max_price", 500.0)),
        )
        return {"enabled": True, "count": len(symbols), "symbols": symbols}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── options ───────────────────────────────────────────────────────────────────

@router.get("/options/chain/{symbol}")
async def option_chain(
    symbol: str,
    request: Request,
    days_out: int = 45,
    type: str = "",
    enrich: bool = True,
):
    contracts = await request.app.state.alpaca.get_option_chain(
        underlying=symbol.upper(),
        days_out=days_out,
        contract_type=type.lower() if type.lower() in ("call", "put") else None,
    )
    if not enrich or not contracts:
        return {"symbol": symbol.upper(), "count": len(contracts), "contracts": contracts}

    syms = [c["symbol"] for c in contracts if c.get("symbol")]
    snapshots = await request.app.state.alpaca.get_option_snapshots(syms)

    enriched = []
    for c in contracts:
        snap = snapshots.get(c["symbol"], {})
        enriched.append({
            **c,
            "delta": snap.get("delta"),
            "gamma": snap.get("gamma"),
            "theta": snap.get("theta"),
            "vega": snap.get("vega"),
            "iv": snap.get("implied_volatility"),
            "bid": snap.get("bid"),
            "ask": snap.get("ask"),
            "mid": snap.get("mid"),
            "spread_pct": snap.get("spread_pct"),
            "volume": snap.get("volume"),
        })
    return {"symbol": symbol.upper(), "count": len(enriched), "contracts": enriched}


class OptionOrderRequest(BaseModel):
    contract_symbol: str
    underlying_symbol: str
    contract_type: str   # "call" | "put"
    expiration_date: str  # "YYYY-MM-DD"
    strike_price: float
    side: str            # "buy" | "sell"
    qty: int


@router.post("/options/order")
async def submit_option_order(req: OptionOrderRequest, request: Request):
    result = await request.app.state.engine.submit_option_order(
        contract_symbol=req.contract_symbol,
        underlying_symbol=req.underlying_symbol,
        contract_type=req.contract_type,
        expiration_date=req.expiration_date,
        strike_price=req.strike_price,
        side=req.side,
        qty=req.qty,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "order failed"))
    return result


@router.get("/options/trades")
async def list_option_trades(request: Request, limit: int = 50):
    return await request.app.state.repo.list_recent_option_trades(limit=limit)


# ── options engine control ────────────────────────────────────────────────────

@router.get("/options/engine/status")
async def options_engine_status(request: Request):
    return request.app.state.options_engine.snapshot_status()


@router.post("/options/engine/pause")
async def options_engine_pause(request: Request):
    request.app.state.options_engine.pause()
    await request.app.state.repo.log_event("options_engine_paused", "via API")
    return {"ok": True, "paused": True}


@router.post("/options/engine/resume")
async def options_engine_resume(request: Request):
    request.app.state.options_engine.resume()
    await request.app.state.repo.log_event("options_engine_resumed", "via API")
    return {"ok": True, "paused": False}


@router.get("/options/positions")
async def list_open_options(request: Request):
    """Open option positions enriched with current Greeks, mid, P&L, DTE, next exit trigger."""
    open_trades = await request.app.state.repo.list_open_option_trades()
    if not open_trades:
        return {"count": 0, "positions": []}

    syms = [t["contract_symbol"] for t in open_trades]
    snapshots = await request.app.state.alpaca.get_option_snapshots(syms)

    from datetime import date as _date
    today = _date.today()

    cfg = request.app.state.config.options
    profit_pct = float(cfg.get("profit_target_pct", 0.50))
    stop_pct = float(cfg.get("stop_loss_pct", 0.50))
    dte_floor = int(cfg.get("dte_floor", 21))

    out = []
    for t in open_trades:
        snap = snapshots.get(t["contract_symbol"], {})
        try:
            exp = _date.fromisoformat(t["expiration_date"])
            dte = (exp - today).days
        except (ValueError, TypeError):
            dte = None
        current_mid = snap.get("mid")
        entry_mid = t.get("entry_mid")
        pnl_pct = None
        if entry_mid and current_mid:
            pnl_pct = (current_mid - entry_mid) / entry_mid

        next_trigger = None
        if dte is not None and dte < dte_floor:
            next_trigger = "dte_floor"
        elif pnl_pct is not None:
            if pnl_pct >= profit_pct:
                next_trigger = "profit_target"
            elif pnl_pct <= -stop_pct:
                next_trigger = "stop_loss"

        out.append({
            **t,
            "current_delta": snap.get("delta"),
            "current_gamma": snap.get("gamma"),
            "current_theta": snap.get("theta"),
            "current_vega": snap.get("vega"),
            "current_iv": snap.get("implied_volatility"),
            "current_bid": snap.get("bid"),
            "current_ask": snap.get("ask"),
            "current_mid": current_mid,
            "dte_remaining": dte,
            "pnl_pct": pnl_pct,
            "next_trigger": next_trigger,
        })

    return {"count": len(out), "positions": out}


@router.get("/options/iv-surface/{symbol}")
async def iv_surface(symbol: str, request: Request, days_out: int = 60):
    """IV-by-strike for nearest 3 expirations. On-demand, no persistence."""
    contracts = await request.app.state.alpaca.get_option_chain(
        underlying=symbol.upper(),
        days_out=days_out,
        contract_type=None,
    )
    if not contracts:
        return {"symbol": symbol.upper(), "expirations": []}

    expirations = sorted({c["expiration_date"] for c in contracts if c.get("expiration_date")})
    expirations = expirations[:3]

    syms = [c["symbol"] for c in contracts if c.get("expiration_date") in expirations]
    snapshots = await request.app.state.alpaca.get_option_snapshots(syms)

    by_exp: dict[str, list[dict]] = {e: [] for e in expirations}
    for c in contracts:
        exp = c.get("expiration_date")
        if exp not in by_exp:
            continue
        snap = snapshots.get(c["symbol"], {})
        if snap.get("implied_volatility") is None:
            continue
        by_exp[exp].append({
            "strike": c["strike_price"],
            "type": c["contract_type"],
            "iv": snap.get("implied_volatility"),
        })

    return {
        "symbol": symbol.upper(),
        "expirations": [
            {
                "expiration_date": e,
                "points": sorted(by_exp[e], key=lambda x: x["strike"]),
            }
            for e in expirations if by_exp[e]
        ],
    }


# ── config ───────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    return request.app.state.config.raw()


@router.post("/config/reload")
async def reload_config(request: Request):
    request.app.state.config.reload()
    await request.app.state.repo.log_event("config_reloaded", "via API")
    return {"ok": True, "config": request.app.state.config.raw()}


class DailyGoalRequest(BaseModel):
    goal: float  # dollar amount; 0 = disabled


@router.post("/config/daily-goal")
async def update_daily_goal(req: DailyGoalRequest, request: Request):
    if req.goal < 0:
        raise HTTPException(400, "goal must be >= 0")
    cfg = request.app.state.config
    text = cfg._path.read_text()
    updated = re.sub(
        r'^(\s+daily_profit_goal:\s*)\S+',
        lambda m: m.group(1) + str(round(req.goal, 2)),
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        # Field missing — insert after daily_loss_limit_pct line
        updated = re.sub(
            r'(^\s+daily_loss_limit_pct:\s*\S+)',
            lambda m: m.group(1) + f'\n  daily_profit_goal: {round(req.goal, 2)}',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    cfg._path.write_text(updated)
    cfg.reload()
    await request.app.state.repo.log_event(
        "daily_goal_updated",
        f"goal=${req.goal:.2f}" if req.goal > 0 else "goal=disabled",
    )
    return {"ok": True, "daily_profit_goal": req.goal}


_VALID_STYLES = {"conservative", "moderate", "aggressive"}


class StyleUpdateRequest(BaseModel):
    style: str


@router.post("/config/style")
async def update_style(req: StyleUpdateRequest, request: Request):
    if req.style not in _VALID_STYLES:
        raise HTTPException(400, f"style must be one of: {', '.join(sorted(_VALID_STYLES))}")
    cfg = request.app.state.config
    text = cfg._path.read_text()
    updated = re.sub(
        r'^(\s+style:\s*)\S+',
        lambda m: m.group(1) + req.style,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        # Field missing — insert after the mode line
        updated = re.sub(
            r'(^\s+mode:\s*\S+)',
            lambda m: m.group(1) + f'\n  style: {req.style}',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    cfg._path.write_text(updated)
    cfg.reload()
    await request.app.state.repo.log_event("style_changed", f"style={req.style}")
    return {"ok": True, "style": req.style}

"""FastAPI routes. All shared resources (alpaca client, engine, repo, config)
are accessed via request.app.state."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
        "live_trading_allowed": risk.live_trading_allowed(),
        "daily_start_equity": risk.daily_start_equity,
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


# ── config ───────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    return request.app.state.config.raw()


@router.post("/config/reload")
async def reload_config(request: Request):
    request.app.state.config.reload()
    await request.app.state.repo.log_event("config_reloaded", "via API")
    return {"ok": True, "config": request.app.state.config.raw()}

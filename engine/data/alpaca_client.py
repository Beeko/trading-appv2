"""Alpaca client wrapping the synchronous alpaca-py SDK in async-friendly methods.

All blocking SDK calls are dispatched via asyncio.to_thread so the FastAPI event
loop is never blocked.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass, OrderSide, QueryOrderStatus, TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest,
)


_TF_MAP = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.paper = paper
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    # ── account / positions ───────────────────────────────────────────────────

    async def get_account(self) -> dict:
        a = await asyncio.to_thread(self.trading.get_account)
        return {
            "id": str(a.id),
            "equity": float(a.equity),
            "last_equity": float(a.last_equity) if a.last_equity else float(a.equity),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "portfolio_value": float(a.portfolio_value),
            "day_trade_count": a.daytrade_count,
            "pattern_day_trader": bool(a.pattern_day_trader),
            "trading_blocked": bool(a.trading_blocked),
            "account_blocked": bool(a.account_blocked),
            "status": str(a.status),
            "currency": str(a.currency),
        }

    async def get_positions(self) -> list[dict]:
        try:
            positions = await asyncio.to_thread(self.trading.get_all_positions)
        except Exception as e:
            logger.error(f"get_positions failed: {e}")
            return []
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "cost_basis": float(p.cost_basis) if p.cost_basis else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc else 0.0,
                "side": str(p.side),
            }
            for p in positions
        ]

    # ── market data ───────────────────────────────────────────────────────────

    async def get_bars(
        self, symbol: str, timeframe: str = "1Day", limit: int = 100
    ) -> Optional[pd.DataFrame]:
        tf = _TF_MAP.get(timeframe, _TF_MAP["1Day"])

        # Buffer date range to ensure we hit `limit` bars after weekends/holidays
        buffer_days = max(limit * 2, 30) if timeframe == "1Day" else max(limit // 4, 5)
        end = datetime.now(timezone.utc) - timedelta(minutes=15)  # respect SIP delay
        start = end - timedelta(days=buffer_days)

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )

        try:
            bars = await asyncio.to_thread(self.data.get_stock_bars, request)
        except Exception as e:
            logger.warning(f"get_bars({symbol}) failed: {e}")
            return None

        df = bars.df
        if df is None or df.empty:
            return None

        # Multi-index (symbol, timestamp) → drop symbol level
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(symbol, level="symbol")
            except KeyError:
                df = df.droplevel(0)

        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].tail(limit)
        return df

    async def get_latest_price(self, symbol: str) -> Optional[float]:
        df = await self.get_bars(symbol, timeframe="1Day", limit=1)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Latest trade price (real-time, no SIP delay) — used for SL/TP anchoring."""
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            resp = await asyncio.to_thread(self.data.get_stock_latest_trade, req)
            trade = resp.get(symbol)
            return float(trade.price) if trade else None
        except Exception as e:
            logger.warning(f"get_current_price({symbol}) failed: {e}")
            return None

    # ── clock ─────────────────────────────────────────────────────────────────

    async def is_market_open(self) -> bool:
        try:
            clock = await asyncio.to_thread(self.trading.get_clock)
            return bool(clock.is_open)
        except Exception as e:
            logger.warning(f"is_market_open failed: {e}")
            return False

    async def get_clock(self) -> dict:
        try:
            c = await asyncio.to_thread(self.trading.get_clock)
            return {
                "is_open": bool(c.is_open),
                "next_open": c.next_open.isoformat() if c.next_open else None,
                "next_close": c.next_close.isoformat() if c.next_close else None,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            }
        except Exception as e:
            logger.warning(f"get_clock failed: {e}")
            return {"is_open": False, "error": str(e)}

    # ── orders ────────────────────────────────────────────────────────────────

    async def submit_market_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: str,
        client_order_id: str,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> dict:
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif_map = {
            "day": TimeInForce.DAY,
            "gtc": TimeInForce.GTC,
            "ioc": TimeInForce.IOC,
            "fok": TimeInForce.FOK,
        }
        tif = tif_map.get(time_in_force.lower(), TimeInForce.DAY)

        is_bracket = (
            side.lower() == "buy"
            and (stop_loss_price is not None or take_profit_price is not None)
        )

        if is_bracket:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.GTC,  # bracket requires GTC
                client_order_id=client_order_id,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2))
                if stop_loss_price else None,
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2))
                if take_profit_price else None,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=tif,
                client_order_id=client_order_id,
            )

        order = await asyncio.to_thread(self.trading.submit_order, req)
        return {
            "id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": order.symbol,
            "status": order.status.value,
            "qty": float(order.qty) if order.qty else 0,
        }

    async def get_orders(
        self, status: str = "all", limit: int = 100
    ) -> list[dict]:
        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }
        req = GetOrdersRequest(
            status=status_map.get(status, QueryOrderStatus.ALL), limit=limit
        )
        orders = await asyncio.to_thread(self.trading.get_orders, req)
        return [
            {
                "id": str(o.id),
                "client_order_id": str(o.client_order_id),
                "symbol": o.symbol,
                "side": str(o.side),
                "qty": float(o.qty) if o.qty else 0,
                "filled_qty": float(o.filled_qty) if o.filled_qty else 0,
                "filled_avg_price": float(o.filled_avg_price)
                if o.filled_avg_price else None,
                "status": str(o.status),
                "order_class": str(o.order_class) if o.order_class else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            }
            for o in orders
        ]

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.to_thread(self.trading.cancel_order_by_id, order_id)

    async def cancel_all_orders(self) -> None:
        await asyncio.to_thread(self.trading.cancel_orders)

    async def close_position(self, symbol: str) -> None:
        await asyncio.to_thread(self.trading.close_position, symbol)

    async def close_all_positions(self) -> None:
        await asyncio.to_thread(self.trading.close_all_positions, cancel_orders=True)

    # ── screener ──────────────────────────────────────────────────────────────

    async def get_most_actives(self, top: int = 50) -> list[str]:
        """Most active US equities by volume."""
        try:
            from alpaca.data.requests import MostActivesRequest
        except ImportError:
            logger.warning("MostActivesRequest unavailable — upgrade alpaca-py")
            return []
        req = MostActivesRequest(top=top, by="volume")
        try:
            resp = await asyncio.to_thread(self.data.get_stock_most_actives, req)
            return [item.symbol for item in (resp.most_actives or [])]
        except Exception as e:
            logger.warning(f"get_most_actives failed: {e}")
            return []

    async def get_top_gainers(self, top: int = 25) -> list[str]:
        """Top gaining US equities by % change today."""
        try:
            from alpaca.data.requests import MoversRequest
        except ImportError:
            logger.warning("MoversRequest unavailable — upgrade alpaca-py")
            return []
        req = MoversRequest(top=top)
        try:
            resp = await asyncio.to_thread(self.data.get_stock_movers, req)
            return [m.symbol for m in (resp.gainers or [])]
        except Exception as e:
            logger.warning(f"get_top_gainers failed: {e}")
            return []

    async def filter_symbols_by_price(
        self, symbols: list[str], min_price: float = 5.0, max_price: float = 500.0
    ) -> list[str]:
        """Return only symbols whose latest trade price is within [min_price, max_price]."""
        if not symbols:
            return []
        filtered: list[str] = []
        for i in range(0, len(symbols), 50):
            batch = symbols[i : i + 50]
            try:
                req = StockLatestTradeRequest(symbol_or_symbols=batch)
                resp = await asyncio.to_thread(self.data.get_stock_latest_trade, req)
                for sym in batch:
                    trade = resp.get(sym)
                    if trade is None:
                        continue
                    price = float(trade.price)
                    if min_price <= price <= max_price:
                        filtered.append(sym)
            except Exception as e:
                logger.warning(f"filter_symbols_by_price batch failed: {e}")
                filtered.extend(batch)  # include on error to avoid silent drops
        return filtered

    # ── tradable assets (used to validate WSB tickers) ────────────────────────

    async def get_tradable_symbols(self) -> set[str]:
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetStatus, AssetClass

        req = GetAssetsRequest(
            status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY
        )
        try:
            assets = await asyncio.to_thread(self.trading.get_all_assets, req)
            return {a.symbol for a in assets if a.tradable}
        except Exception as e:
            logger.error(f"get_tradable_symbols failed: {e}")
            return set()

"""Data access layer. All DB writes/reads go through here."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import desc, func, select

from database.models import (
    DailyPnL, DayTrade, EngineEvent, OptionTrade, ScannerSignal, Trade,
)
from database.session import get_session_factory


class Repository:
    """Thin async wrapper over the SQLAlchemy session factory."""

    # ── trades ────────────────────────────────────────────────────────────────

    async def insert_trade_pending(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        strategy: Optional[str] = None,
        signals: Optional[list[str]] = None,
        signal_score: Optional[int] = None,
        reasoning: Optional[str] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        trading_mode: str = "paper",
        source: str = "auto",
    ) -> int:
        async with get_session_factory()() as s:
            trade = Trade(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                qty=Decimal(str(qty)),
                order_type=order_type,
                status="pending",
                strategy=strategy,
                signals=signals or [],
                signal_score=signal_score,
                reasoning=reasoning,
                stop_loss_price=Decimal(str(stop_loss_price)) if stop_loss_price else None,
                take_profit_price=Decimal(str(take_profit_price)) if take_profit_price else None,
                trading_mode=trading_mode,
                source=source,
            )
            s.add(trade)
            await s.commit()
            await s.refresh(trade)
            return trade.id

    async def update_trade_after_submit(
        self,
        *,
        client_order_id: str,
        broker_order_id: Optional[str],
        status: str,
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(Trade).where(Trade.client_order_id == client_order_id)
            )
            trade = res.scalar_one_or_none()
            if trade is None:
                logger.warning(f"Trade not found for client_order_id={client_order_id}")
                return
            trade.broker_order_id = broker_order_id
            trade.status = status
            await s.commit()

    async def update_trade_fill(
        self,
        *,
        broker_order_id: str,
        filled_qty: float,
        filled_avg_price: float,
        status: str,
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(Trade).where(Trade.broker_order_id == broker_order_id)
            )
            trade = res.scalar_one_or_none()
            if trade is None:
                return
            trade.filled_qty = Decimal(str(filled_qty))
            trade.filled_avg_price = Decimal(str(filled_avg_price))
            trade.status = status
            if status == "filled":
                trade.filled_at = datetime.now(timezone.utc)
            await s.commit()

    async def get_trade_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(Trade).where(Trade.client_order_id == client_order_id)
            )
            t = res.scalar_one_or_none()
            return _trade_to_dict(t) if t else None

    async def list_recent_trades(self, limit: int = 100) -> list[dict]:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(Trade).order_by(desc(Trade.created_at)).limit(limit)
            )
            return [_trade_to_dict(t) for t in res.scalars().all()]

    # ── scanner signals ───────────────────────────────────────────────────────

    async def insert_signal(
        self,
        *,
        symbol: str,
        score: int,
        signals: list[str],
        rsi: float,
        volume_ratio: float,
        price: float,
        action_taken: str,
    ) -> None:
        async with get_session_factory()() as s:
            row = ScannerSignal(
                symbol=symbol,
                score=score,
                signals=signals,
                rsi=Decimal(str(round(rsi, 4))),
                volume_ratio=Decimal(str(round(volume_ratio, 4))),
                price=Decimal(str(round(price, 4))),
                action_taken=action_taken,
            )
            s.add(row)
            await s.commit()

    async def list_recent_signals(self, limit: int = 50) -> list[dict]:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(ScannerSignal)
                .order_by(desc(ScannerSignal.scanned_at))
                .limit(limit)
            )
            return [
                {
                    "symbol": x.symbol,
                    "score": x.score,
                    "signals": x.signals,
                    "rsi": float(x.rsi) if x.rsi is not None else None,
                    "volume_ratio": float(x.volume_ratio) if x.volume_ratio is not None else None,
                    "price": float(x.price) if x.price is not None else None,
                    "action_taken": x.action_taken,
                    "scanned_at": x.scanned_at.isoformat() if x.scanned_at else None,
                }
                for x in res.scalars().all()
            ]

    # ── engine events ─────────────────────────────────────────────────────────

    async def log_event(self, event_type: str, message: str = "") -> None:
        async with get_session_factory()() as s:
            s.add(EngineEvent(event_type=event_type, message=message))
            await s.commit()
        logger.info(f"[event] {event_type}: {message}")

    async def list_recent_events(self, limit: int = 50) -> list[dict]:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(EngineEvent)
                .order_by(desc(EngineEvent.occurred_at))
                .limit(limit)
            )
            return [
                {
                    "event_type": x.event_type,
                    "message": x.message,
                    "occurred_at": x.occurred_at.isoformat() if x.occurred_at else None,
                }
                for x in res.scalars().all()
            ]

    # ── daily P&L ─────────────────────────────────────────────────────────────

    async def upsert_daily_start(self, day: date, starting_equity: float) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(select(DailyPnL).where(DailyPnL.date == day))
            row = res.scalar_one_or_none()
            if row is None:
                s.add(DailyPnL(date=day, starting_equity=Decimal(str(starting_equity))))
            else:
                row.starting_equity = Decimal(str(starting_equity))
            await s.commit()

    async def get_daily_start_equity(self, day: date) -> Optional[float]:
        async with get_session_factory()() as s:
            res = await s.execute(select(DailyPnL).where(DailyPnL.date == day))
            row = res.scalar_one_or_none()
            return float(row.starting_equity) if row and row.starting_equity else None

    async def update_daily_close(
        self, day: date, ending_equity: float, num_trades: int, num_wins: int
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(select(DailyPnL).where(DailyPnL.date == day))
            row = res.scalar_one_or_none()
            if row:
                row.ending_equity = Decimal(str(ending_equity))
                row.num_trades = num_trades
                row.num_wins = num_wins
                if row.starting_equity:
                    row.realized_pnl = Decimal(str(ending_equity)) - row.starting_equity
                await s.commit()

    # ── PDT day-trade tracking ────────────────────────────────────────────────

    async def record_day_trade(self, symbol: str) -> None:
        async with get_session_factory()() as s:
            s.add(DayTrade(symbol=symbol, trade_date=date.today()))
            await s.commit()

    async def count_day_trades_last_5_days(self) -> int:
        cutoff = date.today() - timedelta(days=7)  # 7 calendar covers 5 business
        async with get_session_factory()() as s:
            res = await s.execute(
                select(func.count(DayTrade.id)).where(DayTrade.trade_date >= cutoff)
            )
            return int(res.scalar() or 0)


    # ── option trades ─────────────────────────────────────────────────────────

    async def insert_option_trade_pending(
        self,
        *,
        client_order_id: str,
        contract_symbol: str,
        underlying_symbol: str,
        contract_type: str,
        expiration_date,
        strike_price: float,
        side: str,
        qty: int,
        trading_mode: str = "paper",
    ) -> int:
        async with get_session_factory()() as s:
            row = OptionTrade(
                client_order_id=client_order_id,
                contract_symbol=contract_symbol,
                underlying_symbol=underlying_symbol,
                contract_type=contract_type,
                expiration_date=expiration_date,
                strike_price=Decimal(str(strike_price)),
                side=side,
                qty=qty,
                status="pending",
                trading_mode=trading_mode,
                source="manual",
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row.id

    async def update_option_trade_after_submit(
        self,
        *,
        client_order_id: str,
        broker_order_id: Optional[str],
        status: str,
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(OptionTrade).where(
                    OptionTrade.client_order_id == client_order_id
                )
            )
            row = res.scalar_one_or_none()
            if row is None:
                logger.warning(f"OptionTrade not found: {client_order_id}")
                return
            row.broker_order_id = broker_order_id
            row.status = status
            await s.commit()

    async def list_recent_option_trades(self, limit: int = 50) -> list[dict]:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(OptionTrade)
                .order_by(desc(OptionTrade.created_at))
                .limit(limit)
            )
            return [_option_trade_to_dict(r) for r in res.scalars().all()]

    async def update_option_trade_with_entry_data(
        self,
        *,
        client_order_id: str,
        entry_delta: Optional[float],
        entry_gamma: Optional[float],
        entry_theta: Optional[float],
        entry_vega: Optional[float],
        entry_iv: Optional[float],
        entry_bid: Optional[float],
        entry_ask: Optional[float],
        entry_mid: Optional[float],
        premium_paid: Optional[float],
        dte_at_entry: Optional[int],
        underlying_score: Optional[int],
        underlying_signals: Optional[list[str]],
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(OptionTrade).where(
                    OptionTrade.client_order_id == client_order_id
                )
            )
            row = res.scalar_one_or_none()
            if row is None:
                logger.warning(f"OptionTrade not found for entry data: {client_order_id}")
                return
            row.entry_delta = Decimal(str(entry_delta)) if entry_delta is not None else None
            row.entry_gamma = Decimal(str(entry_gamma)) if entry_gamma is not None else None
            row.entry_theta = Decimal(str(entry_theta)) if entry_theta is not None else None
            row.entry_vega = Decimal(str(entry_vega)) if entry_vega is not None else None
            row.entry_iv = Decimal(str(entry_iv)) if entry_iv is not None else None
            row.entry_bid = Decimal(str(entry_bid)) if entry_bid is not None else None
            row.entry_ask = Decimal(str(entry_ask)) if entry_ask is not None else None
            row.entry_mid = Decimal(str(entry_mid)) if entry_mid is not None else None
            row.premium_paid = Decimal(str(premium_paid)) if premium_paid is not None else None
            row.dte_at_entry = dte_at_entry
            row.underlying_score = underlying_score
            row.underlying_signals = underlying_signals or []
            await s.commit()

    async def update_option_trade_exit(
        self,
        *,
        client_order_id: str,
        exit_mid: float,
        exit_dte: int,
        exit_reason: str,
        status: str = "closing",
    ) -> None:
        async with get_session_factory()() as s:
            res = await s.execute(
                select(OptionTrade).where(
                    OptionTrade.client_order_id == client_order_id
                )
            )
            row = res.scalar_one_or_none()
            if row is None:
                logger.warning(f"OptionTrade not found for exit: {client_order_id}")
                return
            row.exit_mid = Decimal(str(exit_mid))
            row.exit_dte = exit_dte
            row.exit_reason = exit_reason
            row.status = status
            await s.commit()

    async def list_open_option_trades(self) -> list[dict]:
        """Open buy positions awaiting exit (status in {'filled', 'partially_filled'})."""
        async with get_session_factory()() as s:
            res = await s.execute(
                select(OptionTrade)
                .where(
                    OptionTrade.side == "buy",
                    OptionTrade.status.in_(["filled", "partially_filled"]),
                )
                .order_by(desc(OptionTrade.created_at))
            )
            return [_option_trade_to_dict(r) for r in res.scalars().all()]


def _option_trade_to_dict(r: OptionTrade) -> dict:
    return {
        "id": r.id,
        "client_order_id": r.client_order_id,
        "broker_order_id": r.broker_order_id,
        "contract_symbol": r.contract_symbol,
        "underlying_symbol": r.underlying_symbol,
        "contract_type": r.contract_type,
        "expiration_date": str(r.expiration_date) if r.expiration_date else None,
        "strike_price": float(r.strike_price) if r.strike_price else None,
        "side": r.side,
        "qty": r.qty,
        "filled_qty": r.filled_qty,
        "filled_avg_price": float(r.filled_avg_price) if r.filled_avg_price else None,
        "status": r.status,
        "trading_mode": r.trading_mode,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "entry_delta": float(r.entry_delta) if r.entry_delta is not None else None,
        "entry_gamma": float(r.entry_gamma) if r.entry_gamma is not None else None,
        "entry_theta": float(r.entry_theta) if r.entry_theta is not None else None,
        "entry_vega": float(r.entry_vega) if r.entry_vega is not None else None,
        "entry_iv": float(r.entry_iv) if r.entry_iv is not None else None,
        "entry_bid": float(r.entry_bid) if r.entry_bid is not None else None,
        "entry_ask": float(r.entry_ask) if r.entry_ask is not None else None,
        "entry_mid": float(r.entry_mid) if r.entry_mid is not None else None,
        "premium_paid": float(r.premium_paid) if r.premium_paid is not None else None,
        "dte_at_entry": r.dte_at_entry,
        "exit_mid": float(r.exit_mid) if r.exit_mid is not None else None,
        "exit_dte": r.exit_dte,
        "exit_reason": r.exit_reason,
        "underlying_score": r.underlying_score,
        "underlying_signals": r.underlying_signals,
    }


def _trade_to_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "client_order_id": t.client_order_id,
        "broker_order_id": t.broker_order_id,
        "symbol": t.symbol,
        "side": t.side,
        "order_type": t.order_type,
        "qty": float(t.qty) if t.qty else 0,
        "filled_qty": float(t.filled_qty) if t.filled_qty else 0,
        "limit_price": float(t.limit_price) if t.limit_price else None,
        "stop_price": float(t.stop_price) if t.stop_price else None,
        "filled_avg_price": float(t.filled_avg_price) if t.filled_avg_price else None,
        "status": t.status,
        "strategy": t.strategy,
        "signals": t.signals,
        "signal_score": t.signal_score,
        "reasoning": t.reasoning,
        "stop_loss_price": float(t.stop_loss_price) if t.stop_loss_price else None,
        "take_profit_price": float(t.take_profit_price) if t.take_profit_price else None,
        "trading_mode": t.trading_mode,
        "source": t.source,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "filled_at": t.filled_at.isoformat() if t.filled_at else None,
    }

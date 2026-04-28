from datetime import datetime, timezone, date

from sqlalchemy import (
    Column, Integer, String, Numeric, Text, DateTime, Date,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase


def _utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    client_order_id = Column(String(64), unique=True, nullable=False)
    broker_order_id = Column(String(64))
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    order_type = Column(String(20), nullable=False, default="market")
    qty = Column(Numeric(18, 8), nullable=False)
    filled_qty = Column(Numeric(18, 8), default=0)
    limit_price = Column(Numeric(18, 4))
    stop_price = Column(Numeric(18, 4))
    filled_avg_price = Column(Numeric(18, 4))
    status = Column(String(20), nullable=False, default="pending")
    strategy = Column(String(100))
    signals = Column(ARRAY(Text))
    signal_score = Column(Integer)
    reasoning = Column(Text)
    stop_loss_price = Column(Numeric(18, 4))
    take_profit_price = Column(Numeric(18, 4))
    trading_mode = Column(String(10), nullable=False, default="paper")
    source = Column(String(20), default="auto")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    filled_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ScannerSignal(Base):
    __tablename__ = "scanner_signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    score = Column(Integer, nullable=False)
    signals = Column(ARRAY(Text))
    rsi = Column(Numeric(8, 4))
    volume_ratio = Column(Numeric(8, 4))
    price = Column(Numeric(18, 4))
    action_taken = Column(String(50))
    scanned_at = Column(DateTime(timezone=True), default=_utcnow)


class DailyPnL(Base):
    __tablename__ = "daily_pnl"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False, default=date.today)
    starting_equity = Column(Numeric(18, 4))
    ending_equity = Column(Numeric(18, 4))
    realized_pnl = Column(Numeric(18, 4), default=0)
    num_trades = Column(Integer, default=0)
    num_wins = Column(Integer, default=0)


class EngineEvent(Base):
    __tablename__ = "engine_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(String(50), nullable=False)
    message = Column(Text)
    occurred_at = Column(DateTime(timezone=True), default=_utcnow)


class DayTrade(Base):
    __tablename__ = "day_trades"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    trade_date = Column(Date, nullable=False, default=date.today)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class OptionTrade(Base):
    __tablename__ = "option_trades"

    id = Column(Integer, primary_key=True)
    client_order_id = Column(String(64), unique=True, nullable=False)
    broker_order_id = Column(String(64))
    contract_symbol = Column(String(30), nullable=False)
    underlying_symbol = Column(String(20), nullable=False, index=True)
    contract_type = Column(String(10), nullable=False)
    expiration_date = Column(Date, nullable=False)
    strike_price = Column(Numeric(18, 4), nullable=False)
    side = Column(String(10), nullable=False)
    qty = Column(Integer, nullable=False)
    filled_qty = Column(Integer, default=0)
    filled_avg_price = Column(Numeric(18, 4))
    status = Column(String(20), nullable=False, default="pending")
    trading_mode = Column(String(10), nullable=False, default="paper")
    source = Column(String(20), default="manual")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    filled_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # entry snapshot (autonomous flow only)
    entry_delta = Column(Numeric(8, 4))
    entry_gamma = Column(Numeric(8, 4))
    entry_theta = Column(Numeric(8, 4))
    entry_vega = Column(Numeric(8, 4))
    entry_iv = Column(Numeric(8, 4))
    entry_bid = Column(Numeric(18, 4))
    entry_ask = Column(Numeric(18, 4))
    entry_mid = Column(Numeric(18, 4))
    premium_paid = Column(Numeric(18, 4))
    dte_at_entry = Column(Integer)

    # exit data
    exit_mid = Column(Numeric(18, 4))
    exit_dte = Column(Integer)
    exit_reason = Column(String(50))

    # context
    underlying_score = Column(Integer)
    underlying_signals = Column(ARRAY(Text))

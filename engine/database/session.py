from typing import AsyncIterator

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from database.models import Base


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


_MIGRATIONS = [
    # option_trades greeks/exit columns added after initial schema
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_delta        NUMERIC(8, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_gamma        NUMERIC(8, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_theta        NUMERIC(8, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_vega         NUMERIC(8, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_iv           NUMERIC(8, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_bid          NUMERIC(18, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_ask          NUMERIC(18, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_mid          NUMERIC(18, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS premium_paid       NUMERIC(18, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS dte_at_entry       INTEGER",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_mid           NUMERIC(18, 4)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_dte           INTEGER",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_reason        VARCHAR(50)",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS underlying_score   INTEGER",
    "ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS underlying_signals TEXT[]",
]


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))
    logger.info("Database initialized")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _session_factory


async def close_db() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None

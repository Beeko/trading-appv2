"""FastAPI entrypoint. Boots the trading engine as a background asyncio task."""
import asyncio
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.routes import router
from config import TradingConfig, get_settings
from data.alpaca_client import AlpacaClient
from database.repo import Repository
from database.session import close_db, init_db
from risk.manager import RiskManager
from options.engine import OptionsEngine
from trading.engine import TradingEngine


def _configure_logging(log_dir: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | "
                      "<cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}")
    os.makedirs(log_dir, exist_ok=True)
    logger.add(
        os.path.join(log_dir, "engine.log"),
        rotation="50 MB", retention="30 days", level="DEBUG",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_dir)
    logger.info("Starting trading engine application")

    config = TradingConfig(settings.config_file)
    await init_db(settings.database_url)

    repo = Repository()
    alpaca = AlpacaClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
    )
    risk = RiskManager(settings=settings, config=config, repo=repo)
    engine = TradingEngine(
        settings=settings, config=config, client=alpaca, risk=risk, repo=repo,
    )

    # Stash on app.state for routes
    app.state.settings = settings
    app.state.config = config
    app.state.alpaca = alpaca
    app.state.risk = risk
    app.state.repo = repo
    app.state.engine = engine

    options_engine = OptionsEngine(
        settings=settings, config=config, client=alpaca, risk=risk, repo=repo,
    )
    app.state.options_engine = options_engine

    # Test broker connectivity (fail fast on bad creds)
    try:
        acct = await alpaca.get_account()
        logger.info(
            f"Alpaca connected: equity=${acct['equity']:.2f}, "
            f"cash=${acct['cash']:.2f}, paper={settings.alpaca_paper}"
        )
        await repo.log_event("alpaca_connected", f"equity=${acct['equity']:.2f}")
    except Exception as e:
        logger.error(f"Alpaca connection failed: {e}")
        await repo.log_event("alpaca_connection_failed", str(e))

    # Hard guard: refuse to start in live mode unless all three gates align
    if settings.allow_live_trading and not settings.alpaca_paper:
        if config.trading.get("mode") != "live":
            logger.critical(
                "LIVE creds detected but config.yaml mode != 'live' — "
                "refusing to start engine. Set trading.mode: live to confirm."
            )
            engine.pause()

    engine_task = asyncio.create_task(engine.run(), name="trading_engine")
    app.state.engine_task = engine_task

    options_engine_task = asyncio.create_task(
        options_engine.run(), name="options_engine",
    )
    app.state.options_engine_task = options_engine_task

    try:
        yield
    finally:
        logger.info("Shutting down")
        await engine.stop()
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass
        await options_engine.stop()
        options_engine_task.cancel()
        try:
            await options_engine_task
        except asyncio.CancelledError:
            pass
        await close_db()
        logger.info("Shutdown complete")


app = FastAPI(title="Trading Engine", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "trading-engine",
        "version": "0.1.0",
        "docs": "/docs",
        "ui": "http://localhost:8501",
    }

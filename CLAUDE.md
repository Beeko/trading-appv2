# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the stack

```bash
# First-time setup
cp .env.example .env          # fill in ALPACA_API_KEY, ALPACA_SECRET_KEY, DB_PASSWORD

# Build and start all containers
docker compose up -d --build

# Tail engine logs
docker compose logs -f engine

# Restart engine only (after code changes)
docker compose restart engine

# Open a DB shell
docker compose exec db psql -U trader trading
```

Services after startup:
- Streamlit dashboard → http://localhost:8501
- Engine REST API + Swagger docs → http://localhost:8000/docs
- Kill switch from host: `touch state/killswitch` / `rm state/killswitch`

Hot-reload config without restarting: edit `config.yaml`, then `POST /config/reload` or click "Reload config.yaml" in the dashboard.

## Architecture

Three containers orchestrated by `docker-compose.yml`:

```
Streamlit UI (8501) ──HTTP──► FastAPI Engine (8000) ──asyncpg──► PostgreSQL (5432)
                                      │
                              asyncio.to_thread()
                                      │
                               Alpaca SDK (sync)
```

**Engine** (`engine/`) is the core. On startup (`main.py` lifespan), it builds all shared objects and stores them on `app.state`. Routes in `api/routes.py` access everything via `request.app.state` — no dependency injection, no global imports.

The trading loop runs as a single asyncio task alongside uvicorn. It lives in `trading/engine.py:TradingEngine.run()` and ticks every `trading.scan_interval_seconds`.

**Alpaca SDK** is synchronous. Every SDK call is wrapped in `asyncio.to_thread()` inside `data/alpaca_client.py` to avoid blocking the event loop.

**Two config layers** that must not be confused:
- `engine/config.py:Settings` — env-var-only (credentials, DB URL, kill-switch path). Loaded once at startup via `@lru_cache`. Never hot-reloaded.
- `engine/config.py:TradingConfig` — reads `config.yaml`, hot-reloadable via `.reload()`. Strategy parameters, risk limits, watchlist, PDT mode all live here.

## Tick sequence and safety gates

Every tick in `TradingEngine._tick()` checks gates in strict order:

1. Engine paused? → skip
2. Kill switch file exists? → skip
3. Alpaca clock says market closed? → skip
4. Daily equity baseline established (once per calendar day)
5. Daily loss limit breached? → pause engine
6. Account blocked by broker? → pause engine
7. Scan watchlist → score symbols → sort by score descending
8. For each signal above `min_score_to_trade`:
   - Already held / max positions reached? → log and skip
   - Sanity filters (price range, RSI < 80)? → log and skip
   - `RiskManager.size_auto_order()` → compute qty, SL, TP
   - **Write pending trade row to DB first**, then submit to Alpaca, then update row

This write-before-submit pattern is intentional: a crash between submit and update leaves a `pending` row that can be reconciled on restart.

## Live trading gate

Exactly three independent booleans must all be true simultaneously:
- `.env`: `ALLOW_LIVE_TRADING=true`
- `.env`: `ALPACA_PAPER=false`
- `config.yaml`: `trading.mode: live`

`RiskManager.live_trading_allowed()` is the single source of truth. Any mismatch keeps trading in paper mode. On startup, `main.py` additionally pauses the engine if live creds are present but config mode isn't `"live"`.

## Indicators

All indicators in `indicators/technical.py` are hand-rolled pandas (no TA-Lib, no pandas-ta). The functions `macd()`, `rsi()`, `bollinger_bands()`, `ema()` return pandas Series. `calculate_signals(df) -> SignalScore` is the public entrypoint — it computes all indicators and returns a score (roughly -5 to +8) plus a list of human-readable signal strings.

**Scoring weights (current):**
- MACD bullish cross: +2, MACD bullish: +1, MACD bearish: -1
- RSI healthy (40–65): +1, oversold (<30): +1, overbought (>75): -2
- BB breakout + volume: +2, BB breakout alone: +1
- EMA9 > EMA21: +1, else: -1
- Volume surge (>3×): +2, elevated (>1.5×): +1

Requires ≥30 bars; returns `score=-99, signals=["INSUFFICIENT_DATA"]` otherwise.

## Adding a new strategy

1. Create `engine/strategies/my_strategy.py` extending `strategies/base.py:Strategy`
2. Implement `score(symbol, df) -> SignalScore` — reuse functions from `indicators/technical.py`
3. Optionally override `watchlist(default)` to return a dynamic symbol list
4. Swap the instantiation in `trading/engine.py:TradingEngine.__init__`: `self.strategy = MyStrategy()`

No other files need to change.

## Database access

All DB reads/writes go through `database/repo.py:Repository`. It opens a fresh session per call using the factory from `database/session.py`. SQLAlchemy models are in `database/models.py`.

Tables: `trades`, `scanner_signals`, `daily_pnl`, `engine_events`, `day_trades`.

The schema is created by `db/init.sql` (run by Postgres on first container start). SQLAlchemy `create_all` in `session.py:init_db()` acts as a safety net for any tables missed.

## WSB scanner

Disabled by default (`wsb_scanner.enabled: false` in `config.yaml`). To enable:
1. Add `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` to `.env`
2. Add `praw==7.8.1` to `engine/requirements.txt` and rebuild
3. Set `wsb_scanner.enabled: true` in `config.yaml` and reload

`data/reddit_scanner.py:RedditScanner` tries PRAW first, falls back to the public JSON endpoint. Ticker extraction uses a regex + a `_STOPWORDS` set + intersection against Alpaca's tradable asset list (cached in-memory after first fetch).

## Key invariants to preserve

- Risk checks in `RiskManager` run **synchronously** (no awaits in the gate chain) so they cannot be bypassed by async scheduling.
- Every auto-trade writes a `pending` DB row before broker submission — never submit first.
- Manual trades bypass `max_position_pct` but are still blocked by the kill switch and daily loss limit.
- `Settings` (credentials) is cached with `@lru_cache` and never reloaded at runtime — credential changes require a container restart by design.
- The kill switch is a filesystem file (`state/killswitch`), mounted into the container at `/app/state/killswitch`. The UI and API call `RiskManager.activate/deactivate_kill_switch()` which create/delete this file. `touch state/killswitch` from the host also works even when the UI is unreachable.

# Trading App v2 — Autonomous Stock Trading Engine

Multi-container Python trading engine with paper-mode-by-default, technical
indicator confluence, and a Streamlit dashboard.

## Architecture

```
┌──────────────────┐    HTTP    ┌──────────────────┐
│  Streamlit UI    │ ◄────────► │  FastAPI Engine  │
│  (port 8501)     │            │  (port 8000)     │
└──────────────────┘            └─────────┬────────┘
                                          │
                                          ▼
                                ┌──────────────────┐
                                │  PostgreSQL 15   │
                                │  (trades, logs)  │
                                └──────────────────┘
```

Engine talks to **Alpaca** for trading + market data. Optional WSB scanner uses
Reddit (PRAW or public JSON) to feed dynamic watchlists.

## First-time setup

```bash
git clone <this repo> && cd trading-appv2
cp .env.example .env
# Edit .env: paste your Alpaca paper key + secret, set DB_PASSWORD
docker compose up -d --build
```

Then:
- Dashboard → http://localhost:8501
- Engine API docs → http://localhost:8000/docs

## Safety gates

Three independent conditions are required to enable **live trading** — any
single one off keeps you in paper mode:

1. `.env`: `ALLOW_LIVE_TRADING=true`
2. `.env`: `ALPACA_PAPER=false`
3. `config.yaml`: `trading.mode: live`

If credentials are live but config is paper (or vice versa), the engine refuses
to start trading and pauses itself.

## Kill switch

```bash
# Halt all trading immediately (file-based, works even if UI is hung):
touch state/killswitch

# Resume:
rm state/killswitch
```

The dashboard sidebar has the same controls.

## Risk defaults (config.yaml)

| Setting                    | Value | Meaning                            |
|----------------------------|-------|------------------------------------|
| `max_position_pct`         | 2%    | of equity per auto trade           |
| `max_total_positions`      | 10    | symbols held concurrently          |
| `daily_loss_limit_pct`     | 3%    | from open — engine pauses if hit   |
| `stop_loss_pct`            | 5%    | bracket stop-loss below entry      |
| `take_profit_pct`          | 10%   | bracket take-profit above entry    |
| `min_score_to_trade`       | 3     | indicator confluence threshold     |

**Manual trades** bypass the position-size cap (you choose dollar amount) but
are still subject to the kill switch and daily loss limit.

## What's in v1

- ✅ Alpaca paper trading
- ✅ MACD / RSI / Bollinger Bands / EMA crossover / volume confluence scoring
- ✅ Auto position sizing, bracket orders (SL + TP)
- ✅ Daily loss limit + market-hours awareness + PDT counter
- ✅ Kill switch (file + UI button)
- ✅ Manual buy/sell from UI with custom dollar amounts
- ✅ Live & historical scanner signals dashboard
- ✅ Hot-reload config (no restart)
- ✅ Trade log persisted to PostgreSQL
- ⏸ WSB scanner (code present, disabled in config until you add Reddit creds)

## What's coming next (v2+)

- Alpaca market screener (top movers, gappers) feeding the scanner
- Yahoo Finance news scraper + sentiment scoring
- Backtesting harness (vectorbt or backtrader)
- Trailing stops & position-level monitoring
- React dashboard
- TimescaleDB for tick-level history
- Redis pub/sub for inter-component signals

## Project layout

```
trading-appv2/
├─ docker-compose.yml
├─ config.yaml          # hot-reloadable strategy + risk settings
├─ .env.example
├─ db/init.sql          # schema
├─ engine/              # FastAPI + trading loop
│  ├─ main.py
│  ├─ config.py
│  ├─ data/             # Alpaca client, Reddit scanner
│  ├─ indicators/       # MACD, RSI, BB, EMA
│  ├─ risk/             # sizing, kill switch, PDT, daily limit
│  ├─ strategies/       # WSB momentum
│  ├─ trading/          # main engine loop
│  ├─ database/         # SQLAlchemy models, repository
│  └─ api/              # FastAPI routes
├─ ui/                  # Streamlit dashboard
│  └─ app.py
└─ state/               # mounted; holds killswitch flag
```

## Useful commands

```bash
docker compose logs -f engine                  # tail engine logs
docker compose exec db psql -U trader trading  # poke at the DB
docker compose restart engine                  # restart after Dockerfile changes
docker compose down -v                         # nuke (incl. DB volume!)
```

## Enabling the WSB scanner

1. Register a Reddit app at https://www.reddit.com/prefs/apps (type: "script")
2. Add to `.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USER_AGENT=TradingBot/1.0 by your_username
   ```
3. Add `praw==7.8.1` to `engine/requirements.txt` and rebuild
4. In `config.yaml` set `wsb_scanner.enabled: true`
5. Reload config from the dashboard

## Going live (only when paper has been clean for several sessions)

1. Edit `.env`: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to your live keys
2. `.env`: `ALPACA_PAPER=false` and `ALLOW_LIVE_TRADING=true`
3. `config.yaml`: `trading.mode: live`
4. `docker compose restart engine`
5. Watch the engine logs — it logs `mode=live` on startup if all three gates pass

Until then, the engine refuses to send orders to the live Alpaca endpoint.

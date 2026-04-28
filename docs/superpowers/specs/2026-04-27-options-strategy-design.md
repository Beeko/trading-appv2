# Autonomous Options Trading Layer — Design Spec

**Date:** 2026-04-27
**Status:** Approved for implementation planning
**Scope:** Build a fully autonomous, directional single-leg options trading layer alongside the existing stock engine. Manage entry, contract selection, position monitoring, and exits without human intervention. Never exercise contracts.

---

## 1. Goals & Non-Goals

### Goals
- Pivot primary focus from stock day-trading to options trading to escape PDT constraints
- Fully autonomous: scan → score → select contract → execute → monitor → exit, with no human in the loop
- Real-time Greeks (delta, gamma, theta, vega) and IV stored at entry and visible on open positions
- Liquidity filtering on bid-ask spread, volume, and open interest before any contract is considered
- Limit-order execution at mid-price (no market orders) to avoid bid-ask slippage
- Position management with premium-based profit target, premium-based stop loss, and DTE time stop
- Sell-to-close only — contracts must never be exercised or allowed to expire in-the-money

### Non-Goals (deferred)
- Multi-leg strategies (spreads, iron condors, straddles)
- Selling premium / writing options
- IV surface persistence (computed on-demand for v1)
- Backtesting framework for options strategies
- Manual override workflow (existing `/options/order` route stays for emergency manual entry)

---

## 2. High-Level Architecture

A new `OptionsEngine` runs as a second asyncio task in `engine/main.py` lifespan, alongside the existing `TradingEngine` (stocks). Both engines share the same `RiskManager`, `AlpacaClient`, and `Repository` instances via `app.state` — no duplicated risk logic.

```
                    ┌──────────────────────┐
                    │   FastAPI lifespan   │
                    │  (engine/main.py)    │
                    └─────┬────────────┬───┘
                          │            │
            ┌─────────────▼─┐      ┌───▼─────────────┐
            │ TradingEngine │      │  OptionsEngine  │
            │   (stocks)    │      │    (options)    │
            └───────┬───────┘      └────────┬────────┘
                    │                       │
                    └──────┬────────────────┘
                           │ shared
              ┌────────────┼─────────────┐
              ▼            ▼             ▼
         RiskManager  AlpacaClient   Repository
```

The two engines can be paused/resumed independently from the UI. They share kill-switch state, daily loss limit, and daily equity baseline (initialized once per day by whichever engine ticks first).

---

## 3. New & Modified Files

### New files
```
engine/options/
    __init__.py
    engine.py            # OptionsEngine: lifecycle, scan loop, sub-task orchestration
    strategy.py          # OptionsSignal generation on intraday bars
    contract_selector.py # Greeks/IV/liquidity-based contract selection
    position_monitor.py  # Open-position scanner, exit trigger evaluation
```

### Modified files
- `engine/data/alpaca_client.py` — add `OptionHistoricalDataClient`, snapshot methods, limit order method
- `engine/database/models.py` — extend `OptionTrade` with Greeks/IV/exit columns
- `engine/database/repo.py` — add open-option-position queries
- `engine/api/routes.py` — add options engine status/control routes; enrich existing `/options/chain` with Greeks
- `engine/main.py` — start `OptionsEngine.run()` as a parallel asyncio task
- `engine/config.py` — surface new `options:` config section via `TradingConfig`
- `config.yaml` — new `options:` section
- `db/init.sql` — update `option_trades` schema for new columns
- `ui/streamlit_app.py` (or equivalent) — Options tab enhancements

---

## 4. Data Layer

### Source: Alpaca `OptionHistoricalDataClient`

Each option snapshot returns:
- **Greeks:** `delta`, `gamma`, `theta`, `vega`
- **`implied_volatility`**
- **`latest_quote`:** `bid_price`, `ask_price`, `bid_size`, `ask_size`
- **`latest_trade`:** last price, volume

### New `AlpacaClient` methods
| Method | Purpose |
|---|---|
| `get_option_snapshot(contract_symbol)` | Single-contract snapshot |
| `get_option_snapshots(contract_symbols: list[str])` | Batched (≤100 per call) |
| `submit_option_limit_order(contract_symbol, qty, side, limit_price, client_order_id)` | Limit order entry/exit |
| `cancel_option_order(broker_order_id)` | Used by limit-order timeout watcher |

### Caching
- 30-second TTL on snapshot batches keyed by `(underlying, expiry_window)` to avoid hammering the data API during a 5-minute scan cycle
- Snapshot cache is in-memory only (no DB persistence)

### Liquidity filter
A contract is rejected before any selection logic runs if any of the following fail:
- `bid_price > 0` (no one-sided markets)
- `(ask - bid) / mid ≤ liquidity.max_spread_pct` (default 0.20)
- `volume ≥ liquidity.min_volume` (default 10)
- `open_interest ≥ liquidity.min_open_interest` (default 100)

### IV surface
For v1, IV is logged per-contract at entry (`entry_iv` column). The `GET /options/iv-surface/{symbol}` route computes IV-by-strike on demand for the nearest 2-3 expirations from cached snapshots. No separate IV time-series persistence.

---

## 5. Strategy Module (`strategy.py`)

### Signal generation
- Reuses `indicators/technical.py:calculate_signals()` on **5min bars** (lookback 78 bars ≈ 1 trading day)
- Returns `OptionsSignal(symbol, direction, score, signals, price, rsi, volume_ratio)`
- `direction` ∈ {`bullish`, `bearish`, `neutral`}

### Bearish scoring extension
The existing scorer is bullish-only. Add a symmetric bearish scorer (same module, new function):
- MACD bearish cross: +2
- MACD bearish: +1
- RSI overbought (>75): +1
- RSI extreme overbought (>85): +1
- BB upper-band breakdown + volume: +2
- EMA9 < EMA21: +1
- Volume surge (>3×) accompanying a down candle: +2

A signal triggers:
- `bullish` if `bullish_score ≥ min_score_to_trade`
- `bearish` if `bearish_score ≥ min_score_to_trade`
- `neutral` otherwise

If both scores cross threshold simultaneously (rare), the higher-scoring direction wins; tied scores produce `neutral` (the market is genuinely choppy).

---

## 6. Contract Selector (`contract_selector.py`)

Pipeline executed in strict order — never relax filters to force a trade:

1. **Fetch chain** — `AlpacaClient.get_option_chain(underlying, days_out=max_dte)`, filter to `contract_type` matching signal direction (call for bullish, put for bearish)
2. **DTE filter** — keep contracts with `min_dte ≤ DTE ≤ max_dte` (defaults 28/45). `min_dte` is required to be `> dte_floor` so a freshly-opened position is never immediately in the time-stop zone.
3. **Snapshot batch** — single `get_option_snapshots(remaining_symbols)` call for Greeks/IV/bid-ask
4. **Liquidity filter** — drop contracts failing spread/volume/OI gates
5. **Delta filter** — keep contracts with `|delta - target_delta| ≤ delta_tolerance` (default 0.40 ± 0.05 for calls, -0.40 ± 0.05 for puts)
6. **Pick winner** — surviving contract whose delta is closest to `target_delta`. Tie-breaker: higher `open_interest`

If no contract survives, log a `signal_no_eligible_contract` event with the rejection reason and skip the signal.

### Returned object
```python
@dataclass
class SelectedContract:
    contract_symbol: str
    underlying_symbol: str
    contract_type: str          # "call" | "put"
    expiration_date: date
    strike_price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    volume: int
    open_interest: int
    dte: int
```

---

## 7. Options Engine (`engine.py`)

### Lifecycle
- `run()` starts the entry-scan loop and spawns `position_monitor.run()` as a sub-task
- `pause()` / `resume()` flip an internal flag (independent of stock engine)
- `stop()` sets a stop event; both sub-tasks observe it
- On startup: scan all open option positions; any with `DTE < dte_floor` are scheduled for immediate sell-to-close before the regular monitor loop begins

### Tick sequence (`scan_interval_seconds`, default 300s)
1. Engine paused? → skip
2. Kill switch active? → skip
3. Market closed? → skip
4. `RiskManager.initialize_daily_baseline()` (no-op if already done by stock engine)
5. Daily loss breached? → pause options engine
6. Account blocked? → pause options engine
7. Build watchlist (reuse `TradingEngine._build_watchlist()` logic — screener > WSB > static)
8. Scan each symbol → 5min bars → `OptionsSignal`
9. For each signal with `direction != neutral` and `score ≥ min_score_to_trade`:
   - Skip if engine already holds an option in this underlying
   - Skip if `len(open_option_positions) ≥ max_option_positions`
   - `ContractSelector.select(...)` → `SelectedContract` or `None`
   - If `None`: log skip and continue
   - Compute `qty` = `floor((equity × max_position_pct) / (mid × 100))`; skip if `qty < 1`
   - **Write pending DB row first**, then submit limit order at mid-price, then update row
10. Refresh account state after each successful order

### Independent of stock engine
- The stock engine continues to scan stocks and place equity orders
- Both engines respect the same kill switch and daily loss limit
- Position counts are tracked separately: `max_total_positions` (stocks) vs `max_option_positions` (options)

---

## 8. Position Monitor (`position_monitor.py`)

Runs as a sub-task of `OptionsEngine`, ticking every `monitor_interval_seconds` (default 60s).

### Each tick
1. Fetch all positions from Alpaca
2. Filter to options (cross-reference `option_trades` table by `contract_symbol`)
3. Batch `get_option_snapshots(contract_symbols)` for Greeks/bid-ask
4. For each open option position, compute:
   - `current_mid = (bid + ask) / 2`
   - `pnl_pct = (current_mid - entry_mid) / entry_mid`
   - `dte_remaining`
5. Evaluate exit triggers in order:
   1. **DTE floor** — `dte_remaining < dte_floor` → exit, reason `dte_floor`
   2. **Profit target** — `pnl_pct ≥ profit_target_pct` → exit, reason `profit_target`
   3. **Stop loss** — `pnl_pct ≤ -stop_loss_pct` → exit, reason `stop_loss`
6. If any trigger fires:
   - Submit `submit_option_limit_order(side="sell", limit_price=current_mid, ...)`
   - On success: update `option_trades` row with `exit_mid`, `exit_dte`, `exit_reason`, `status="closing"`

### Limit order timeout
A separate watcher (sub-task of monitor) tracks pending entries and exits:
- **Entry timeout:** unfilled after `limit_order.fill_timeout_seconds` (default 120s) → cancel, mark trade `cancelled`, retry on next scan
- **Exit timeout:** unfilled after timeout → cancel, re-submit at `mid × (1 - exit_retry_price_step_pct)` (i.e., 5% worse), up to `exit_retry_max` (default 3) retries. Exits must complete to enforce risk rules.

### Risk gate interactions
- **Kill switch active:** entry scan skipped; **exits still fire** (we want to close positions, not strand them)
- **Daily loss breached:** entry scan skipped; exits still fire
- **Position monitor never blocks on a single contract** — failures on one contract are logged and the loop moves on to the next

---

## 9. Never-Exercise Guarantee

Three independent layers:

1. **Code-level clamp:** `dte_floor = max(1, config.options.dte_floor)`. Even if config is corrupted to 0 or negative, the floor is at least 1 trading day before expiry.
2. **Entry filter:** contract selection rejects anything with `DTE ≤ dte_floor`, so the engine never opens a position already in (or one trading day from) the time-stop zone. Combined with `min_dte > dte_floor`, this is enforced both by config validation at startup and by the selector itself.
3. **Startup sweep:** on `OptionsEngine.run()` startup, every open option position is checked; any with `DTE < dte_floor` is immediately scheduled for sell-to-close before the regular monitor loop runs.

If all three layers fail, Alpaca's broker-side auto-liquidation around expiry is the final backstop — but the design assumes layers 1-3 always succeed.

---

## 10. Database Schema

### `option_trades` — additive schema changes

| New column | Type | Purpose |
|---|---|---|
| `entry_delta` | Numeric(8, 4) | Greek snapshot at entry |
| `entry_gamma` | Numeric(8, 4) | Greek snapshot at entry |
| `entry_theta` | Numeric(8, 4) | Greek snapshot at entry |
| `entry_vega` | Numeric(8, 4) | Greek snapshot at entry |
| `entry_iv` | Numeric(8, 4) | IV at entry |
| `entry_bid` | Numeric(18, 4) | Bid at entry |
| `entry_ask` | Numeric(18, 4) | Ask at entry |
| `entry_mid` | Numeric(18, 4) | (bid+ask)/2 at entry — basis for P&L math |
| `premium_paid` | Numeric(18, 4) | `entry_mid * 100 * qty` |
| `dte_at_entry` | Integer | DTE on the day of entry |
| `exit_mid` | Numeric(18, 4) | Mid at exit fill |
| `exit_dte` | Integer | DTE at exit |
| `exit_reason` | String(50) | `profit_target` \| `stop_loss` \| `dte_floor` \| `manual` |
| `underlying_score` | Integer | Score that triggered the entry |
| `underlying_signals` | ARRAY(Text) | Signal list at entry |

Existing rows get NULL for new columns — these are historical and won't be re-evaluated.

`db/init.sql` updated to include new columns; SQLAlchemy `create_all` covers fresh installs.

---

## 11. Configuration (`config.yaml`)

```yaml
options:
  enabled: true
  scan_interval_seconds: 300          # 5 min between full chain scans
  monitor_interval_seconds: 60        # 1 min between exit-trigger checks
  bar_timeframe: "5Min"
  lookback_bars: 78                   # ~1 trading day of 5min bars
  min_score_to_trade: 3
  max_option_positions: 5
  max_position_pct: 0.02              # 2% of equity per contract position
  target_delta: 0.40
  delta_tolerance: 0.05               # 0.35–0.45 acceptance window
  min_dte: 28                         # must be > dte_floor (validated at startup)
  max_dte: 45
  profit_target_pct: 0.50             # close at +50% premium gain
  stop_loss_pct: 0.50                 # close at -50% premium loss
  dte_floor: 21                       # force-close when DTE < this
  liquidity:
    max_spread_pct: 0.20              # (ask-bid)/mid threshold
    min_volume: 10
    min_open_interest: 100
  limit_order:
    fill_timeout_seconds: 120
    exit_retry_max: 3
    exit_retry_price_step_pct: 0.05
```

All values hot-reloadable via `POST /config/reload`.

---

## 12. API Routes

### New
| Method | Path | Purpose |
|---|---|---|
| GET | `/options/engine/status` | running, paused, open count, last scan timestamp, last error |
| POST | `/options/engine/pause` | Pause options engine only |
| POST | `/options/engine/resume` | Resume options engine only |
| GET | `/options/positions` | Open option positions enriched with current Greeks, mid, P&L %, DTE, nearest exit trigger |
| GET | `/options/iv-surface/{symbol}` | IV-by-strike for nearest 2-3 expirations (on-demand) |

### Modified
- `GET /options/chain/{symbol}` — enriched response includes Greeks, IV, bid, ask, mid, spread%, volume from snapshot batch
- `POST /options/order` — kept as-is for emergency manual override

---

## 13. UI Changes (Streamlit)

### Options tab — three sub-views

1. **Open positions**
   - Columns: contract, side, qty, entry mid, current mid, P&L %, entry delta, current delta, current IV, DTE remaining, nearest exit trigger
   - Color-coded P&L, near-trigger alerts in yellow

2. **Chain explorer** (enhanced existing view)
   - Columns: strike, type, expiry, delta, gamma, theta, vega, IV, bid, ask, mid, spread %, volume, OI
   - Toggle: "Hide illiquid" applies the same filter the engine uses

3. **IV surface**
   - Line chart: strike (x) vs IV (y), one line per expiration, nearest 3 expirations

### Engine controls panel
- Add a second status row: Options engine (running/paused, open count, last scan)
- Independent pause/resume buttons

### Live signals tab
- New columns: "Option entry triggered?" (yes/no), "Selected contract" (contract symbol or skip reason)

---

## 14. Risk & Failure Modes

| Failure mode | Mitigation |
|---|---|
| Data API outage | Snapshot calls fail → log, skip scan, retry next interval. Position monitor falls back to most recent snapshot for ≤5 min before forcing a "stale data — pause new entries" mode |
| Limit order never fills | Entry: cancel after 120s, skip signal. Exit: cancel, re-price 5% worse, up to 3 retries, then escalate to manual via `engine_events` log |
| Account loses options trading permission mid-session | First Alpaca rejection → pause options engine, log `options_permission_lost` event |
| Bid/ask = 0/0 (no quotes) | Liquidity filter rejects pre-selection; for open positions, monitor flags the contract and waits one tick before retrying |
| DB write fails between pending insert and broker submit | Pending row exists with `status=pending` and no `broker_order_id` — startup reconciliation reads these rows, queries Alpaca by `client_order_id`, and updates accordingly |
| Both engines try to flip kill switch simultaneously | Kill switch is file-based and idempotent — both `activate` calls land on the same file, no race |

---

## 15. Open Questions / Future Work

- **Spread strategies** — when IV is elevated, single-leg longs are expensive; spreads would help. Deferred to v2.
- **Greeks-based exit triggers** — exit when delta drifts far from entry (thesis broken) is more nuanced than premium-based exits. Deferred until baseline is proven.
- **Multi-account / partial size scaling** — current design assumes one account, max sizing per signal. Larger accounts may want to scale into positions.
- **Backtesting** — no historical options bars in v1; can add when Alpaca exposes a historical options-bars endpoint or when we integrate Polygon.
- **Pattern day trader interaction** — opening and closing the same option contract intraday counts as a day trade. Need to verify Alpaca's PDT counter increments for options the same way; if so, the existing PDT tracker should cover it. To be confirmed during implementation.

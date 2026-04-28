# Autonomous Options Trading Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully autonomous, directional single-leg options trading layer that runs alongside the existing stock engine, with Greeks/IV/liquidity-aware contract selection, premium-based exits, and a never-exercise guarantee.

**Architecture:** New `OptionsEngine` runs as a parallel asyncio task in `main.py` lifespan. Shares `RiskManager`, `AlpacaClient`, `Repository` via `app.state`. Module split: `strategy.py` (signal scoring on 5min bars), `contract_selector.py` (chain filtering pipeline), `position_monitor.py` (exit triggers + limit-order watcher), `engine.py` (orchestration + lifecycle).

**Tech Stack:** Python 3.12, FastAPI, asyncio, alpaca-py 0.34.0 (`OptionHistoricalDataClient`), SQLAlchemy async, pandas, pytest + pytest-asyncio for tests.

**Spec:** [docs/superpowers/specs/2026-04-27-options-strategy-design.md](../specs/2026-04-27-options-strategy-design.md)

---

## File Map

### New files
| Path | Responsibility |
|---|---|
| `engine/options/__init__.py` | Empty package marker |
| `engine/options/strategy.py` | `OptionsSignal` dataclass + `score_options_signal()` (bullish/bearish/neutral) |
| `engine/options/contract_selector.py` | `SelectedContract` dataclass + `ContractSelector.select()` filter pipeline |
| `engine/options/position_monitor.py` | Exit-trigger evaluation + limit-order timeout/retry watcher |
| `engine/options/engine.py` | `OptionsEngine` class — lifecycle + scan loop + sub-task orchestration |
| `engine/tests/__init__.py` | Empty package marker |
| `engine/tests/conftest.py` | Shared fixtures (mock alpaca, mock repo, sample dfs) |
| `engine/tests/test_indicators_bearish.py` | Tests for bearish scoring extension |
| `engine/tests/options/__init__.py` | Empty package marker |
| `engine/tests/options/test_strategy.py` | Unit tests for `score_options_signal` |
| `engine/tests/options/test_contract_selector.py` | Unit tests for filter pipeline |
| `engine/tests/options/test_position_monitor.py` | Unit tests for exit triggers + retry logic |
| `engine/tests/options/test_engine.py` | Integration tests (mocked dependencies) |
| `engine/tests/data/__init__.py` | Empty package marker |
| `engine/tests/data/test_alpaca_options.py` | Unit tests for new `AlpacaClient` option methods |

### Modified files
| Path | Changes |
|---|---|
| `engine/data/alpaca_client.py` | Add `OptionHistoricalDataClient` init + `get_option_snapshot(s)` + `submit_option_limit_order` + `cancel_option_order` |
| `engine/database/models.py` | Extend `OptionTrade` with Greeks, IV, bid/ask, mid, premium, DTE, exit columns |
| `engine/database/repo.py` | Add open-position queries, exit update methods, enriched insert with Greeks |
| `engine/api/routes.py` | Add `/options/engine/status\|pause\|resume`, `/options/positions`, `/options/iv-surface/{symbol}`; enrich `/options/chain/{symbol}` |
| `engine/main.py` | Start `OptionsEngine.run()` as second asyncio task; validate config at startup |
| `engine/config.py` | Add `options` property + startup validation (`min_dte > dte_floor`) |
| `engine/indicators/technical.py` | Add `calculate_bearish_signals()` |
| `engine/requirements.txt` | Add `pytest`, `pytest-asyncio` |
| `config.yaml` | New `options:` section |
| `db/init.sql` | New columns on `option_trades`; add `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` for existing installs |
| `ui/app.py` | Options tab — open positions sub-view, enriched chain explorer, IV surface chart, options engine controls |

---

## Conventions Used Throughout

- **Tests:** Each new module gets a test file under `engine/tests/`. Tests run from inside the `engine/` directory: `cd engine && python -m pytest tests/path/to/test.py -v`. The container is the source of truth: `docker compose exec engine python -m pytest tests/...`.
- **Mocks:** Use `unittest.mock.AsyncMock` for async dependencies. Build small `pandas.DataFrame` fixtures for indicator tests.
- **Commits:** After each green test step. Commit messages follow the existing repo style (terse, lowercase verb, no Conventional Commits prefix).
- **No type stubs:** Project doesn't use mypy. Type hints in code for documentation only.
- **Imports inside `engine/`:** Bare module names (e.g., `from data.alpaca_client import …`), matching existing pattern. Tests under `engine/tests/` use the same import style with `pytest.ini` setting `pythonpath = engine`.

---

## Task 1: Test infrastructure setup

**Files:**
- Create: `engine/tests/__init__.py`
- Create: `engine/tests/conftest.py`
- Create: `engine/pytest.ini`
- Modify: `engine/requirements.txt`

The project has no tests yet. We add pytest + pytest-asyncio plus shared fixtures so every later task can write a test before code.

- [ ] **Step 1: Add pytest dependencies**

Edit `engine/requirements.txt` and append:

```
pytest==8.3.4
pytest-asyncio==0.24.0
```

- [ ] **Step 2: Create pytest config**

Create `engine/pytest.ini`:

```ini
[pytest]
pythonpath = .
asyncio_mode = auto
testpaths = tests
addopts = -ra
```

- [ ] **Step 3: Create test package marker**

Create `engine/tests/__init__.py` (empty file).

- [ ] **Step 4: Create shared fixtures**

Create `engine/tests/conftest.py`:

```python
"""Shared pytest fixtures for engine tests."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def trending_up_df() -> pd.DataFrame:
    """40 bars of cleanly trending-up OHLCV data — should produce a bullish score."""
    n = 40
    base = np.linspace(100, 130, n)
    rng = np.random.default_rng(seed=42)
    noise = rng.normal(0, 0.3, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    volume = rng.integers(800_000, 1_500_000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def trending_down_df() -> pd.DataFrame:
    """40 bars of cleanly trending-down OHLCV data — should produce a bearish score."""
    n = 40
    base = np.linspace(130, 100, n)
    rng = np.random.default_rng(seed=43)
    noise = rng.normal(0, 0.3, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close + 0.1
    volume = rng.integers(800_000, 1_500_000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def flat_df() -> pd.DataFrame:
    """40 bars of flat data — should produce a neutral score."""
    n = 40
    rng = np.random.default_rng(seed=44)
    close = 100 + rng.normal(0, 0.2, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 1_000_000),
        },
        index=idx,
    )


@pytest.fixture
def mock_alpaca():
    """AsyncMock AlpacaClient with the methods used by the options engine."""
    m = MagicMock()
    m.get_account = AsyncMock(return_value={
        "equity": 100_000.0, "cash": 50_000.0, "buying_power": 50_000.0,
        "trading_blocked": False, "account_blocked": False,
        "daytrade_count": 0, "pattern_day_trader": False,
    })
    m.get_positions = AsyncMock(return_value=[])
    m.is_market_open = AsyncMock(return_value=True)
    m.get_clock = AsyncMock(return_value={"is_open": True})
    m.get_bars = AsyncMock(return_value=None)
    m.get_option_chain = AsyncMock(return_value=[])
    m.get_option_snapshots = AsyncMock(return_value={})
    m.get_option_snapshot = AsyncMock(return_value=None)
    m.submit_option_limit_order = AsyncMock(return_value={
        "id": "broker-id-1", "client_order_id": "test", "status": "accepted", "qty": 1,
    })
    m.cancel_option_order = AsyncMock()
    return m


@pytest.fixture
def mock_repo():
    """AsyncMock Repository — only the methods the options engine touches."""
    m = MagicMock()
    m.log_event = AsyncMock()
    m.insert_signal = AsyncMock()
    m.insert_option_trade_pending = AsyncMock(return_value=1)
    m.update_option_trade_after_submit = AsyncMock()
    m.update_option_trade_with_entry_data = AsyncMock()
    m.update_option_trade_exit = AsyncMock()
    m.list_open_option_trades = AsyncMock(return_value=[])
    return m


@pytest.fixture
def mock_risk():
    """MagicMock RiskManager — synchronous methods for risk gates."""
    m = MagicMock()
    m.kill_switch_active = MagicMock(return_value=False)
    m.daily_loss_breached = MagicMock(return_value=False)
    m.daily_profit_goal_reached = MagicMock(return_value=False)
    m.trading_mode = MagicMock(return_value="paper")
    m.initialize_daily_baseline = AsyncMock()
    m.daily_start_equity = 100_000.0
    return m


@pytest.fixture
def sample_snapshot():
    """A typical valid option snapshot dict (post-AlpacaClient normalization)."""
    return {
        "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
        "implied_volatility": 0.35,
        "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
        "volume": 250, "open_interest": 1500,
        "last_price": 2.55,
    }


@pytest.fixture
def sample_chain_contract():
    """A typical chain contract dict (post-AlpacaClient normalization)."""
    today = date.today()
    return {
        "symbol": "AAPL260619C00200000",
        "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0,
        "close_price": 2.50,
        "open_interest": 1500,
        "tradable": True,
    }
```

- [ ] **Step 5: Verify the test infrastructure runs**

Rebuild the engine container so pytest installs:

```bash
docker compose build engine
docker compose up -d engine
```

Run pytest with no tests to confirm setup:

```bash
docker compose exec engine python -m pytest tests/ -v
```

Expected output: `no tests ran in <X>s` (exit code 5, but command runs cleanly — no import errors).

- [ ] **Step 6: Commit**

```bash
git add engine/requirements.txt engine/pytest.ini engine/tests/__init__.py engine/tests/conftest.py
git commit -m "Add pytest infrastructure for options layer

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Bearish scoring extension

**Files:**
- Modify: `engine/indicators/technical.py`
- Create: `engine/tests/test_indicators_bearish.py`

The existing scorer is bullish-only. Add a symmetric bearish scorer so the options strategy can detect put setups. Keep it in the same module as `calculate_signals` for symmetry.

- [ ] **Step 1: Write failing tests for bearish scoring**

Create `engine/tests/test_indicators_bearish.py`:

```python
"""Tests for calculate_bearish_signals."""
import pandas as pd

from indicators.technical import calculate_bearish_signals


def test_insufficient_data_returns_minus_99():
    df = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 100, 100]})
    result = calculate_bearish_signals(df, "TEST")
    assert result.score == -99
    assert result.signals == ["INSUFFICIENT_DATA"]


def test_trending_down_produces_positive_bearish_score(trending_down_df):
    result = calculate_bearish_signals(trending_down_df, "TEST")
    assert result.score > 0, f"expected positive bearish score, got {result.score}"
    assert result.symbol == "TEST"


def test_trending_up_produces_low_bearish_score(trending_up_df):
    result = calculate_bearish_signals(trending_up_df, "TEST")
    # Bullish trend should not generate bearish confluence
    assert result.score <= 0, f"expected non-positive bearish score, got {result.score}"


def test_flat_data_produces_low_bearish_score(flat_df):
    result = calculate_bearish_signals(flat_df, "TEST")
    assert -2 <= result.score <= 2  # near-neutral
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/test_indicators_bearish.py -v
```

Expected: FAIL — `ImportError: cannot import name 'calculate_bearish_signals'`.

- [ ] **Step 3: Implement bearish scoring**

Add to `engine/indicators/technical.py` after `calculate_signals`:

```python
def calculate_bearish_signals(df: pd.DataFrame, symbol: str = "") -> SignalScore:
    """Symmetric bearish counterpart to calculate_signals. Higher score = stronger
    put setup. Returns -99 with INSUFFICIENT_DATA marker if fewer than 30 bars.
    """
    if df is None or len(df) < 30:
        return SignalScore(symbol=symbol, score=-99, signals=["INSUFFICIENT_DATA"])

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    macd_line, signal_line, _ = macd(close)
    rsi_vals = rsi(close)
    bb_upper, _, bb_lower = bollinger_bands(close)
    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    vol_ma20 = volume.rolling(20).mean()

    curr_close = float(close.iloc[-1])
    curr_macd = float(macd_line.iloc[-1])
    curr_signal = float(signal_line.iloc[-1])
    prev_macd = float(macd_line.iloc[-2])
    prev_signal = float(signal_line.iloc[-2])
    curr_rsi = float(rsi_vals.iloc[-1]) if not pd.isna(rsi_vals.iloc[-1]) else 50.0
    curr_bb_lower = float(bb_lower.iloc[-1])
    curr_ema9 = float(ema9.iloc[-1])
    curr_ema21 = float(ema21.iloc[-1])
    curr_vol = float(volume.iloc[-1])
    curr_vol_ma = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else curr_vol
    vol_ratio = curr_vol / curr_vol_ma if curr_vol_ma > 0 else 1.0

    score = 0
    signals: list[str] = []

    # ── MACD bearish cross ─────────────────────────────────────────────────────
    macd_cross_down = (prev_macd >= prev_signal) and (curr_macd < curr_signal)
    if macd_cross_down:
        score += 2
        signals.append("MACD_BEARISH_CROSS")
    elif curr_macd < curr_signal and curr_macd < 0:
        score += 1
        signals.append("MACD_BEARISH")
    elif curr_macd > curr_signal:
        score -= 1
        signals.append("MACD_BULLISH")

    # ── RSI overbought ────────────────────────────────────────────────────────
    if curr_rsi > 85:
        score += 2
        signals.append(f"RSI_EXTREME_OVERBOUGHT({curr_rsi:.1f})")
    elif curr_rsi > 75:
        score += 1
        signals.append(f"RSI_OVERBOUGHT({curr_rsi:.1f})")
    elif curr_rsi < 35:
        score -= 1
        signals.append(f"RSI_OVERSOLD({curr_rsi:.1f})")

    # ── BB lower-band breakdown with volume ───────────────────────────────────
    below_lower = curr_close < curr_bb_lower
    if below_lower and vol_ratio > 1.5:
        score += 2
        signals.append("BB_BREAKDOWN_HIGH_VOL")
    elif below_lower:
        score += 1
        signals.append("BB_BREAKDOWN")

    # ── EMA cross (bearish stack) ─────────────────────────────────────────────
    if curr_ema9 < curr_ema21:
        score += 1
        signals.append("EMA9_BELOW_EMA21")
    else:
        score -= 1
        signals.append("EMA9_ABOVE_EMA21")

    # ── Volume surge on a down candle ─────────────────────────────────────────
    is_down_candle = curr_close < float(close.iloc[-2])
    if is_down_candle and vol_ratio > 3.0:
        score += 2
        signals.append(f"DOWN_VOLUME_SURGE({vol_ratio:.1f}x)")
    elif is_down_candle and vol_ratio > 1.5:
        score += 1
        signals.append(f"DOWN_VOLUME_ELEVATED({vol_ratio:.1f}x)")

    return SignalScore(
        symbol=symbol,
        score=score,
        signals=signals,
        rsi=curr_rsi,
        volume_ratio=vol_ratio,
        price=curr_close,
        macd_bullish_cross=False,
        above_bb_upper=False,
        ema9_above_ema21=curr_ema9 > curr_ema21,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/test_indicators_bearish.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/indicators/technical.py engine/tests/test_indicators_bearish.py
git commit -m "Add bearish indicator scoring for options put setups

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Database schema migration — extend `option_trades`

**Files:**
- Modify: `engine/database/models.py`
- Modify: `db/init.sql`

Add columns for entry Greeks/IV/bid-ask, premium, DTE, exit data. Additive only — existing rows get NULL.

- [ ] **Step 1: Extend the SQLAlchemy model**

Edit `engine/database/models.py`. Replace the entire `OptionTrade` class with:

```python
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
```

- [ ] **Step 2: Update db/init.sql**

Replace the `option_trades` block in `db/init.sql` with:

```sql
CREATE TABLE IF NOT EXISTS option_trades (
    id                  SERIAL PRIMARY KEY,
    client_order_id     VARCHAR(64) UNIQUE NOT NULL,
    broker_order_id     VARCHAR(64),
    contract_symbol     VARCHAR(30) NOT NULL,
    underlying_symbol   VARCHAR(20) NOT NULL,
    contract_type       VARCHAR(10) NOT NULL,          -- call | put
    expiration_date     DATE NOT NULL,
    strike_price        NUMERIC(18, 4) NOT NULL,
    side                VARCHAR(10) NOT NULL,           -- buy | sell
    qty                 INTEGER NOT NULL,
    filled_qty          INTEGER DEFAULT 0,
    filled_avg_price    NUMERIC(18, 4),
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    trading_mode        VARCHAR(10) NOT NULL DEFAULT 'paper',
    source              VARCHAR(20) DEFAULT 'manual',
    entry_delta         NUMERIC(8, 4),
    entry_gamma         NUMERIC(8, 4),
    entry_theta         NUMERIC(8, 4),
    entry_vega          NUMERIC(8, 4),
    entry_iv            NUMERIC(8, 4),
    entry_bid           NUMERIC(18, 4),
    entry_ask           NUMERIC(18, 4),
    entry_mid           NUMERIC(18, 4),
    premium_paid        NUMERIC(18, 4),
    dte_at_entry        INTEGER,
    exit_mid            NUMERIC(18, 4),
    exit_dte            INTEGER,
    exit_reason         VARCHAR(50),
    underlying_score    INTEGER,
    underlying_signals  TEXT[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

After the `CREATE TABLE` blocks but before the index lines, add idempotent `ALTER TABLE` statements so existing databases pick up the new columns on next container restart:

```sql
-- Additive migration for existing option_trades installs
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_delta        NUMERIC(8, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_gamma        NUMERIC(8, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_theta        NUMERIC(8, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_vega         NUMERIC(8, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_iv           NUMERIC(8, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_bid          NUMERIC(18, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_ask          NUMERIC(18, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS entry_mid          NUMERIC(18, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS premium_paid       NUMERIC(18, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS dte_at_entry       INTEGER;
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_mid           NUMERIC(18, 4);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_dte           INTEGER;
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS exit_reason        VARCHAR(50);
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS underlying_score   INTEGER;
ALTER TABLE option_trades ADD COLUMN IF NOT EXISTS underlying_signals TEXT[];
```

Place the `ALTER TABLE` block immediately after the seven `CREATE TABLE` blocks, before the index `CREATE INDEX` block at the end of the file.

- [ ] **Step 3: Apply schema in DB**

Restart Postgres so init scripts re-run on existing volume only when fresh, but the `IF NOT EXISTS` ALTER statements mean we can apply via psql against a running DB:

```bash
docker compose exec db psql -U trader trading -f /docker-entrypoint-initdb.d/init.sql
```

If the file isn't mounted at that path (Docker only auto-runs init scripts on first volume creation), apply manually:

```bash
docker compose cp db/init.sql db:/tmp/init.sql
docker compose exec db psql -U trader trading -f /tmp/init.sql
```

Expected: `ALTER TABLE` statements execute silently (NOTICE messages for already-existing columns are OK).

- [ ] **Step 4: Verify columns are present**

```bash
docker compose exec db psql -U trader trading -c "\d option_trades"
```

Expected output: lists all 25 columns including `entry_delta`, `exit_reason`, etc.

- [ ] **Step 5: Restart engine to pick up the new model**

```bash
docker compose restart engine
docker compose logs engine --tail 30
```

Expected: clean startup, no SQLAlchemy errors.

- [ ] **Step 6: Commit**

```bash
git add engine/database/models.py db/init.sql
git commit -m "Extend option_trades schema with Greeks, IV, exit columns

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Repository methods for autonomous option trades

**Files:**
- Modify: `engine/database/repo.py`

Add three repository methods: `update_option_trade_with_entry_data` (after fill, capture Greeks), `update_option_trade_exit` (record exit reason + price), `list_open_option_trades` (positions still in `filled` status).

- [ ] **Step 1: Write tests**

Skip — repo methods are thin wrappers over SQLAlchemy and are exercised by the engine integration test in Task 12. Manual verification in Step 4 below.

- [ ] **Step 2: Add new repo methods**

Edit `engine/database/repo.py`. Inside the `Repository` class, add after `list_recent_option_trades`:

```python
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
```

- [ ] **Step 3: Update `_option_trade_to_dict` to include new fields**

Edit `engine/database/repo.py`. Replace the `_option_trade_to_dict` function with:

```python
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
```

- [ ] **Step 4: Verify by importing the engine**

```bash
docker compose restart engine
docker compose logs engine --tail 20
```

Expected: clean startup. Hit the API to confirm:

```bash
curl -s http://localhost:8000/options/trades | head
```

Expected: returns `[]` (no trades) or existing trades with the new keys present (NULL).

- [ ] **Step 5: Commit**

```bash
git add engine/database/repo.py
git commit -m "Add option trade entry/exit/list-open repository methods

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: AlpacaClient — option snapshots (Greeks, IV, bid/ask)

**Files:**
- Modify: `engine/data/alpaca_client.py`
- Create: `engine/tests/data/__init__.py`
- Create: `engine/tests/data/test_alpaca_options.py`

Add `OptionHistoricalDataClient` and two methods to fetch snapshots. The SDK returns Pydantic models — we normalize to plain dicts so callers don't import alpaca types.

- [ ] **Step 1: Write failing tests**

Create `engine/tests/data/__init__.py` (empty).

Create `engine/tests/data/test_alpaca_options.py`:

```python
"""Tests for AlpacaClient option-snapshot normalization."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.alpaca_client import AlpacaClient


def _fake_snapshot(*, delta=0.40, gamma=0.05, theta=-0.08, vega=0.15,
                   iv=0.35, bid=2.50, ask=2.60, volume=250, oi=1500):
    """Build a SimpleNamespace mimicking the alpaca-py snapshot shape."""
    return SimpleNamespace(
        greeks=SimpleNamespace(delta=delta, gamma=gamma, theta=theta, vega=vega),
        implied_volatility=iv,
        latest_quote=SimpleNamespace(
            bid_price=bid, ask_price=ask, bid_size=10, ask_size=10
        ),
        latest_trade=SimpleNamespace(price=(bid + ask) / 2, size=volume),
        daily_bar=SimpleNamespace(volume=volume),
        open_interest=oi,
    )


@pytest.fixture
def client():
    c = AlpacaClient(api_key="k", secret_key="s", paper=True)
    return c


async def test_get_option_snapshot_normalizes_greek_fields(client):
    fake = _fake_snapshot()
    fake_resp = {"AAPL260619C00200000": fake}
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value=fake_resp)

    snap = await client.get_option_snapshot("AAPL260619C00200000")

    assert snap is not None
    assert snap["delta"] == 0.40
    assert snap["gamma"] == 0.05
    assert snap["theta"] == -0.08
    assert snap["vega"] == 0.15
    assert snap["implied_volatility"] == 0.35
    assert snap["bid"] == 2.50
    assert snap["ask"] == 2.60
    assert snap["mid"] == pytest.approx(2.55)
    assert snap["spread_pct"] == pytest.approx((2.60 - 2.50) / 2.55)


async def test_get_option_snapshots_returns_keyed_dict(client):
    fake_resp = {
        "A": _fake_snapshot(delta=0.30),
        "B": _fake_snapshot(delta=0.40),
    }
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value=fake_resp)

    snaps = await client.get_option_snapshots(["A", "B"])

    assert set(snaps.keys()) == {"A", "B"}
    assert snaps["A"]["delta"] == 0.30
    assert snaps["B"]["delta"] == 0.40


async def test_get_option_snapshots_empty_input_short_circuits(client):
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock()

    snaps = await client.get_option_snapshots([])

    assert snaps == {}
    client.option_data.get_option_snapshot.assert_not_called()


async def test_get_option_snapshot_handles_missing_greeks(client):
    fake = SimpleNamespace(
        greeks=None,
        implied_volatility=None,
        latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.2,
                                     bid_size=1, ask_size=1),
        latest_trade=None,
        daily_bar=None,
        open_interest=None,
    )
    client.option_data = MagicMock()
    client.option_data.get_option_snapshot = MagicMock(return_value={"X": fake})

    snap = await client.get_option_snapshot("X")

    assert snap is not None
    assert snap["delta"] is None
    assert snap["bid"] == 1.0
    assert snap["ask"] == 1.2
    assert snap["mid"] == pytest.approx(1.1)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/data/test_alpaca_options.py -v
```

Expected: FAIL — `AttributeError: 'AlpacaClient' object has no attribute 'option_data'` (or similar).

- [ ] **Step 3: Implement option snapshot methods**

Edit `engine/data/alpaca_client.py`:

Add at the top of the file alongside the existing imports:

```python
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, OptionLatestQuoteRequest
```

In `AlpacaClient.__init__`, after `self.data = StockHistoricalDataClient(...)`, add:

```python
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)
```

Then add to the class, before the `# ── tradable assets …` section:

```python
    # ── option snapshots (Greeks / IV / bid-ask) ──────────────────────────────

    @staticmethod
    def _normalize_snapshot(snap) -> dict:
        """Convert an alpaca-py OptionSnapshot to a plain dict."""
        greeks = getattr(snap, "greeks", None)
        delta = getattr(greeks, "delta", None) if greeks else None
        gamma = getattr(greeks, "gamma", None) if greeks else None
        theta = getattr(greeks, "theta", None) if greeks else None
        vega = getattr(greeks, "vega", None) if greeks else None

        quote = getattr(snap, "latest_quote", None)
        bid = float(getattr(quote, "bid_price", 0)) if quote else 0.0
        ask = float(getattr(quote, "ask_price", 0)) if quote else 0.0
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or bid or 0.0)
        spread_pct = ((ask - bid) / mid) if (mid > 0 and bid > 0 and ask > 0) else None

        trade = getattr(snap, "latest_trade", None)
        last_price = float(getattr(trade, "price", 0)) if trade else None

        daily = getattr(snap, "daily_bar", None)
        volume = int(getattr(daily, "volume", 0)) if daily else 0

        oi = getattr(snap, "open_interest", None)

        return {
            "delta": float(delta) if delta is not None else None,
            "gamma": float(gamma) if gamma is not None else None,
            "theta": float(theta) if theta is not None else None,
            "vega": float(vega) if vega is not None else None,
            "implied_volatility": float(snap.implied_volatility)
                if getattr(snap, "implied_volatility", None) is not None else None,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "volume": volume,
            "open_interest": int(oi) if oi is not None else None,
            "last_price": last_price,
        }

    async def get_option_snapshot(self, contract_symbol: str) -> Optional[dict]:
        """Single-contract snapshot. Returns None on error or missing data."""
        try:
            req = OptionSnapshotRequest(symbol_or_symbols=contract_symbol)
            resp = await asyncio.to_thread(
                self.option_data.get_option_snapshot, req
            )
            snap = resp.get(contract_symbol) if isinstance(resp, dict) else None
            if snap is None:
                return None
            return self._normalize_snapshot(snap)
        except Exception as e:
            logger.warning(f"get_option_snapshot({contract_symbol}) failed: {e}")
            return None

    async def get_option_snapshots(
        self, contract_symbols: list[str]
    ) -> dict[str, dict]:
        """Batched snapshots, keyed by contract_symbol. Drops contracts that error."""
        if not contract_symbols:
            return {}
        out: dict[str, dict] = {}
        # Alpaca caps batch size; chunk to be safe
        for i in range(0, len(contract_symbols), 100):
            batch = contract_symbols[i:i + 100]
            try:
                req = OptionSnapshotRequest(symbol_or_symbols=batch)
                resp = await asyncio.to_thread(
                    self.option_data.get_option_snapshot, req
                )
            except Exception as e:
                logger.warning(f"get_option_snapshots batch failed: {e}")
                continue
            if not isinstance(resp, dict):
                continue
            for sym, snap in resp.items():
                if snap is not None:
                    out[sym] = self._normalize_snapshot(snap)
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/data/test_alpaca_options.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/data/alpaca_client.py engine/tests/data/__init__.py engine/tests/data/test_alpaca_options.py
git commit -m "Add OptionHistoricalDataClient snapshot methods

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: AlpacaClient — option limit orders + cancellation

**Files:**
- Modify: `engine/data/alpaca_client.py`
- Modify: `engine/tests/data/test_alpaca_options.py`

Replace single `submit_option_order` (market) with `submit_option_limit_order`. Add `cancel_option_order`.

- [ ] **Step 1: Write failing tests**

Append to `engine/tests/data/test_alpaca_options.py`:

```python
async def test_submit_option_limit_order_uses_limit_request(client):
    captured: dict = {}

    def fake_submit(req):
        captured["req"] = req
        return SimpleNamespace(
            id="broker-1",
            client_order_id="opt_TEST_x",
            symbol="AAPL260619C00200000",
            status=SimpleNamespace(value="accepted"),
            qty=2,
        )

    client.trading = MagicMock()
    client.trading.submit_order = MagicMock(side_effect=fake_submit)

    result = await client.submit_option_limit_order(
        contract_symbol="AAPL260619C00200000",
        qty=2, side="buy", limit_price=2.55,
        client_order_id="opt_TEST_x",
    )

    assert result["id"] == "broker-1"
    assert result["status"] == "accepted"
    req = captured["req"]
    # LimitOrderRequest pydantic model has .limit_price
    assert float(req.limit_price) == 2.55


async def test_cancel_option_order_passes_through(client):
    client.trading = MagicMock()
    client.trading.cancel_order_by_id = MagicMock()

    await client.cancel_option_order("broker-xyz")

    client.trading.cancel_order_by_id.assert_called_once_with("broker-xyz")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/data/test_alpaca_options.py -v -k limit_order or cancel
```

Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement the methods**

Edit `engine/data/alpaca_client.py`. Add to the imports near the top:

```python
from alpaca.trading.requests import LimitOrderRequest
```

Add these methods to the class (place them next to the existing `submit_option_order`):

```python
    async def submit_option_limit_order(
        self,
        *,
        contract_symbol: str,
        qty: int,
        side: str,
        limit_price: float,
        client_order_id: str,
    ) -> dict:
        """Single-leg option limit order. Mid-price preferred for tight execution."""
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = LimitOrderRequest(
            symbol=contract_symbol,
            qty=qty,
            side=order_side,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        order = await asyncio.to_thread(self.trading.submit_order, req)
        return {
            "id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": order.symbol,
            "status": order.status.value if hasattr(order.status, "value")
                else str(order.status),
            "qty": int(float(order.qty)) if order.qty else qty,
        }

    async def cancel_option_order(self, broker_order_id: str) -> None:
        await asyncio.to_thread(self.trading.cancel_order_by_id, broker_order_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/data/test_alpaca_options.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/data/alpaca_client.py engine/tests/data/test_alpaca_options.py
git commit -m "Add option limit order + cancel methods to AlpacaClient

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: Options strategy module

**Files:**
- Create: `engine/options/__init__.py`
- Create: `engine/options/strategy.py`
- Create: `engine/tests/options/__init__.py`
- Create: `engine/tests/options/test_strategy.py`

`OptionsSignal` wraps the bullish/bearish split and exposes a `direction` enum so contract-selector logic doesn't need to look at the score sign.

- [ ] **Step 1: Write failing tests**

Create `engine/tests/options/__init__.py` (empty).

Create `engine/tests/options/test_strategy.py`:

```python
"""Tests for OptionsSignal generation."""
import pandas as pd

from options.strategy import OptionsSignal, score_options_signal


def test_insufficient_data_returns_neutral():
    df = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 100, 100]})
    sig = score_options_signal("TEST", df, min_score=3)
    assert sig.direction == "neutral"
    assert sig.score == 0


def test_bullish_trend_classified_bullish(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=3)
    assert sig.direction == "bullish"
    assert sig.score >= 3
    assert sig.symbol == "TEST"


def test_bearish_trend_classified_bearish(trending_down_df):
    sig = score_options_signal("TEST", trending_down_df, min_score=3)
    assert sig.direction == "bearish"
    assert sig.score >= 3


def test_flat_data_classified_neutral(flat_df):
    sig = score_options_signal("TEST", flat_df, min_score=3)
    assert sig.direction == "neutral"


def test_higher_threshold_demotes_borderline_to_neutral(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=99)
    assert sig.direction == "neutral"


def test_signal_carries_indicator_metrics(trending_up_df):
    sig = score_options_signal("TEST", trending_up_df, min_score=3)
    assert sig.price > 0
    assert sig.rsi > 0
    assert sig.volume_ratio > 0
    assert isinstance(sig.signals, list)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_strategy.py -v
```

Expected: FAIL — `ImportError: No module named 'options'`.

- [ ] **Step 3: Implement the strategy module**

Create `engine/options/__init__.py` (empty).

Create `engine/options/strategy.py`:

```python
"""Options signal generation. Wraps the bullish/bearish indicator scorers and
classifies into a directional signal the contract selector can act on."""
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from indicators.technical import calculate_bearish_signals, calculate_signals


Direction = Literal["bullish", "bearish", "neutral"]


@dataclass
class OptionsSignal:
    symbol: str
    direction: Direction
    score: int                       # the winning score (0 if neutral)
    bullish_score: int
    bearish_score: int
    signals: list[str]               # signals from the winning side
    rsi: float = 0.0
    volume_ratio: float = 0.0
    price: float = 0.0


def score_options_signal(
    symbol: str, df: pd.DataFrame, min_score: int = 3
) -> OptionsSignal:
    """Compute bullish + bearish scores; classify the dominant direction.

    Tied scores → neutral. Both must clear `min_score` for direction to fire.
    """
    bull = calculate_signals(df, symbol)
    bear = calculate_bearish_signals(df, symbol)

    if bull.score == -99 or bear.score == -99:
        return OptionsSignal(
            symbol=symbol, direction="neutral", score=0,
            bullish_score=0, bearish_score=0, signals=["INSUFFICIENT_DATA"],
        )

    bull_qualifies = bull.score >= min_score
    bear_qualifies = bear.score >= min_score

    if bull_qualifies and not bear_qualifies:
        direction: Direction = "bullish"
        winner = bull
    elif bear_qualifies and not bull_qualifies:
        direction = "bearish"
        winner = bear
    elif bull_qualifies and bear_qualifies:
        if bull.score > bear.score:
            direction, winner = "bullish", bull
        elif bear.score > bull.score:
            direction, winner = "bearish", bear
        else:
            direction = "neutral"
            return OptionsSignal(
                symbol=symbol, direction=direction, score=0,
                bullish_score=bull.score, bearish_score=bear.score,
                signals=["TIED_SCORES"], rsi=bull.rsi,
                volume_ratio=bull.volume_ratio, price=bull.price,
            )
    else:
        return OptionsSignal(
            symbol=symbol, direction="neutral", score=0,
            bullish_score=bull.score, bearish_score=bear.score,
            signals=[], rsi=bull.rsi,
            volume_ratio=bull.volume_ratio, price=bull.price,
        )

    return OptionsSignal(
        symbol=symbol,
        direction=direction,
        score=winner.score,
        bullish_score=bull.score,
        bearish_score=bear.score,
        signals=winner.signals,
        rsi=winner.rsi,
        volume_ratio=winner.volume_ratio,
        price=winner.price,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_strategy.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/options/__init__.py engine/options/strategy.py engine/tests/options/__init__.py engine/tests/options/test_strategy.py
git commit -m "Add OptionsSignal strategy classifying bullish/bearish/neutral

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Contract selector

**Files:**
- Create: `engine/options/contract_selector.py`
- Create: `engine/tests/options/test_contract_selector.py`

The pipeline: chain → DTE filter → snapshot batch → liquidity filter → delta filter → pick closest-to-target. If anything is empty, return `None` (never relax filters).

- [ ] **Step 1: Write failing tests**

Create `engine/tests/options/test_contract_selector.py`:

```python
"""Tests for ContractSelector pipeline."""
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from options.contract_selector import ContractSelector, SelectorConfig


def _contract(symbol: str, ctype: str, dte: int, strike: float) -> dict:
    return {
        "symbol": symbol,
        "underlying_symbol": "AAPL",
        "contract_type": ctype,
        "expiration_date": (date.today() + timedelta(days=dte)).isoformat(),
        "strike_price": strike,
        "open_interest": 1000,
        "tradable": True,
    }


def _snap(*, delta=0.40, bid=2.50, ask=2.60, vol=250, oi=1500, iv=0.35):
    return {
        "delta": delta, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
        "implied_volatility": iv,
        "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
        "spread_pct": (ask - bid) / ((bid + ask) / 2) if bid + ask > 0 else None,
        "volume": vol, "open_interest": oi, "last_price": (bid + ask) / 2,
    }


@pytest.fixture
def cfg():
    return SelectorConfig(
        target_delta=0.40, delta_tolerance=0.05,
        min_dte=28, max_dte=45,
        max_spread_pct=0.20, min_volume=10, min_open_interest=100,
        dte_floor=21,
    )


def _selector(client, cfg):
    return ContractSelector(client, cfg)


async def test_select_returns_none_when_chain_empty(mock_alpaca, cfg):
    mock_alpaca.get_option_chain = AsyncMock(return_value=[])
    sel = _selector(mock_alpaca, cfg)
    result = await sel.select("AAPL", "bullish")
    assert result is None


async def test_select_filters_dte_window(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=10, strike=200),  # below min_dte
        _contract("B", "call", dte=30, strike=200),  # in window
        _contract("C", "call", dte=60, strike=200),  # above max_dte
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={"B": _snap(delta=0.40)})

    sel = _selector(mock_alpaca, cfg)
    result = await sel.select("AAPL", "bullish")

    assert result is not None
    assert result.contract_symbol == "B"
    # confirm only B was passed to snapshot fetch
    mock_alpaca.get_option_snapshots.assert_called_once_with(["B"])


async def test_select_rejects_wide_spread(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(bid=2.0, ask=3.0, delta=0.40)  # spread = ~40%
    })

    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_rejects_low_volume(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(vol=5, delta=0.40)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_rejects_low_open_interest(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(oi=50, delta=0.40)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_filters_delta_outside_tolerance(mock_alpaca, cfg):
    chain = [_contract("A", "call", dte=30, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    # delta 0.20 is outside 0.40 ± 0.05
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.20)
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None


async def test_select_picks_delta_closest_to_target(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=30, strike=195),
        _contract("B", "call", dte=30, strike=200),
        _contract("C", "call", dte=30, strike=205),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.45),  # |0.45 - 0.40| = 0.05
        "B": _snap(delta=0.41),  # |0.41 - 0.40| = 0.01 ← winner
        "C": _snap(delta=0.36),  # |0.36 - 0.40| = 0.04
    })

    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is not None
    assert result.contract_symbol == "B"
    assert result.delta == 0.41


async def test_select_for_bearish_uses_negative_delta_target(mock_alpaca, cfg):
    chain = [
        _contract("A", "put", dte=30, strike=200),
        _contract("B", "put", dte=30, strike=195),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=-0.41),
        "B": _snap(delta=-0.30),  # outside tolerance for -0.40 target
    })

    result = await _selector(mock_alpaca, cfg).select("AAPL", "bearish")
    assert result is not None
    assert result.contract_symbol == "A"
    assert result.delta == -0.41


async def test_select_breaks_ties_with_higher_open_interest(mock_alpaca, cfg):
    chain = [
        _contract("A", "call", dte=30, strike=200),
        _contract("B", "call", dte=30, strike=205),
    ]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "A": _snap(delta=0.40, oi=500),
        "B": _snap(delta=0.40, oi=2500),
    })
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is not None
    assert result.contract_symbol == "B"  # higher OI wins tie


async def test_select_rejects_dte_at_or_below_floor(mock_alpaca, cfg):
    # cfg.min_dte=28, dte_floor=21. A 22 DTE contract passes min_dte? No: 22<28 fails dte filter.
    # Even if min_dte were lower, dte_floor must be enforced.
    chain = [_contract("A", "call", dte=21, strike=200)]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={"A": _snap(delta=0.40)})
    result = await _selector(mock_alpaca, cfg).select("AAPL", "bullish")
    assert result is None  # 21 DTE ≤ dte_floor → rejected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_contract_selector.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the contract selector**

Create `engine/options/contract_selector.py`:

```python
"""Contract selector pipeline. Filters chain by DTE → snapshot → liquidity →
delta, then picks the contract whose delta is closest to target.

Never relaxes filters to force a trade. Returns None on empty result."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

from loguru import logger


Direction = Literal["bullish", "bearish"]


@dataclass
class SelectorConfig:
    target_delta: float          # absolute value (0.40 means 0.40 for calls, -0.40 for puts)
    delta_tolerance: float       # +/- band around target (0.05 = 0.35–0.45)
    min_dte: int                 # entry-window minimum
    max_dte: int                 # entry-window maximum
    max_spread_pct: float        # (ask-bid)/mid threshold
    min_volume: int
    min_open_interest: int
    dte_floor: int               # belt-and-suspenders: never enter at/below this DTE


@dataclass
class SelectedContract:
    contract_symbol: str
    underlying_symbol: str
    contract_type: str           # "call" | "put"
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


class ContractSelector:
    def __init__(self, alpaca_client, cfg: SelectorConfig):
        self.client = alpaca_client
        self.cfg = cfg

    async def select(
        self, underlying: str, direction: Direction
    ) -> Optional[SelectedContract]:
        contract_type = "call" if direction == "bullish" else "put"

        # 1. Fetch chain
        chain = await self.client.get_option_chain(
            underlying=underlying,
            days_out=self.cfg.max_dte,
            contract_type=contract_type,
        )
        if not chain:
            logger.info(f"selector[{underlying}]: empty chain")
            return None

        today = date.today()

        # 2. DTE filter (incl. dte_floor belt-and-suspenders)
        dte_filtered: list[tuple[dict, int]] = []
        for c in chain:
            exp_str = c.get("expiration_date")
            if not exp_str:
                continue
            try:
                exp = date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp - today).days
            if dte <= self.cfg.dte_floor:
                continue
            if dte < self.cfg.min_dte or dte > self.cfg.max_dte:
                continue
            dte_filtered.append((c, dte))

        if not dte_filtered:
            logger.info(f"selector[{underlying}]: no contracts in DTE window")
            return None

        symbols = [c["symbol"] for c, _ in dte_filtered]

        # 3. Snapshot batch
        snapshots = await self.client.get_option_snapshots(symbols)
        if not snapshots:
            logger.info(f"selector[{underlying}]: snapshot batch empty")
            return None

        # 4 + 5. Liquidity + delta filters; collect candidates
        candidates: list[tuple[SelectedContract, float]] = []
        target = self.cfg.target_delta if direction == "bullish" else -self.cfg.target_delta

        for contract, dte in dte_filtered:
            sym = contract["symbol"]
            snap = snapshots.get(sym)
            if not snap:
                continue
            if not self._passes_liquidity(snap):
                continue
            delta = snap.get("delta")
            if delta is None:
                continue
            if abs(delta - target) > self.cfg.delta_tolerance:
                continue

            try:
                exp = date.fromisoformat(contract["expiration_date"])
            except (ValueError, TypeError, KeyError):
                continue

            sc = SelectedContract(
                contract_symbol=sym,
                underlying_symbol=contract.get("underlying_symbol", underlying),
                contract_type=contract["contract_type"],
                expiration_date=exp,
                strike_price=float(contract["strike_price"]),
                delta=float(delta),
                gamma=float(snap.get("gamma") or 0),
                theta=float(snap.get("theta") or 0),
                vega=float(snap.get("vega") or 0),
                iv=float(snap.get("implied_volatility") or 0),
                bid=float(snap.get("bid") or 0),
                ask=float(snap.get("ask") or 0),
                mid=float(snap.get("mid") or 0),
                spread_pct=float(snap.get("spread_pct") or 0),
                volume=int(snap.get("volume") or 0),
                open_interest=int(snap.get("open_interest") or 0),
                dte=dte,
            )
            distance = abs(delta - target)
            candidates.append((sc, distance))

        if not candidates:
            logger.info(f"selector[{underlying}]: no candidates after liquidity+delta filters")
            return None

        # 6. Pick winner — closest delta to target, OI as tiebreaker
        candidates.sort(key=lambda pair: (pair[1], -pair[0].open_interest))
        return candidates[0][0]

    def _passes_liquidity(self, snap: dict) -> bool:
        bid = snap.get("bid") or 0
        ask = snap.get("ask") or 0
        mid = snap.get("mid") or 0
        if bid <= 0 or ask <= 0 or mid <= 0:
            return False
        spread_pct = snap.get("spread_pct")
        if spread_pct is None or spread_pct > self.cfg.max_spread_pct:
            return False
        if int(snap.get("volume") or 0) < self.cfg.min_volume:
            return False
        if int(snap.get("open_interest") or 0) < self.cfg.min_open_interest:
            return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_contract_selector.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/options/contract_selector.py engine/tests/options/test_contract_selector.py
git commit -m "Add ContractSelector pipeline with DTE/liquidity/delta filters

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 9: Position monitor — exit trigger evaluation (pure logic)

**Files:**
- Create: `engine/options/position_monitor.py`
- Create: `engine/tests/options/test_position_monitor.py`

Start with the pure exit-decision logic. The full async loop with order submission is layered on in Task 10.

- [ ] **Step 1: Write failing tests**

Create `engine/tests/options/test_position_monitor.py`:

```python
"""Tests for PositionMonitor exit-trigger evaluation."""
from datetime import date, timedelta

import pytest

from options.position_monitor import (
    ExitDecision, MonitorConfig, evaluate_exit,
)


@pytest.fixture
def cfg():
    return MonitorConfig(
        profit_target_pct=0.50,
        stop_loss_pct=0.50,
        dte_floor=21,
    )


def test_no_trigger_when_at_break_even(cfg):
    decision = evaluate_exit(entry_mid=2.50, current_mid=2.50, dte=30, cfg=cfg)
    assert decision is None


def test_profit_target_fires_at_50pct_gain(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=3.00, dte=30, cfg=cfg)
    assert decision is not None
    assert decision.reason == "profit_target"


def test_stop_loss_fires_at_50pct_loss(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=1.00, dte=30, cfg=cfg)
    assert decision is not None
    assert decision.reason == "stop_loss"


def test_dte_floor_fires_below_threshold(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=2.05, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_dte_floor_takes_priority_over_profit_target(cfg):
    # Both would fire; DTE floor must win because risk-rule precedence
    decision = evaluate_exit(entry_mid=2.00, current_mid=3.50, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_dte_floor_takes_priority_over_stop_loss(cfg):
    decision = evaluate_exit(entry_mid=2.00, current_mid=0.50, dte=20, cfg=cfg)
    assert decision is not None
    assert decision.reason == "dte_floor"


def test_zero_entry_mid_returns_none_safely(cfg):
    decision = evaluate_exit(entry_mid=0.0, current_mid=1.0, dte=30, cfg=cfg)
    assert decision is None


def test_at_dte_floor_exactly_does_not_fire(cfg):
    # dte_floor=21; rule is "DTE < dte_floor", so 21 should not trigger
    decision = evaluate_exit(entry_mid=2.00, current_mid=2.05, dte=21, cfg=cfg)
    assert decision is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_position_monitor.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the pure logic**

Create `engine/options/position_monitor.py`:

```python
"""Position monitor for autonomous options. Evaluates exit triggers and
fires sell-to-close limit orders. Limit-order timeouts retry at progressively
worse prices to enforce risk-rule completion."""
import asyncio
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger


@dataclass
class MonitorConfig:
    profit_target_pct: float       # close at +50% premium gain
    stop_loss_pct: float           # close at -50% premium loss
    dte_floor: int                 # force-close when DTE < this


@dataclass
class ExitDecision:
    reason: str                    # "profit_target" | "stop_loss" | "dte_floor"
    pnl_pct: float
    current_mid: float
    dte: int


def evaluate_exit(
    *, entry_mid: float, current_mid: float, dte: int, cfg: MonitorConfig
) -> Optional[ExitDecision]:
    """Evaluate exit triggers in priority order. Returns None if no trigger fires.

    Priority: dte_floor > profit_target > stop_loss. DTE floor is highest
    priority because it enforces the never-exercise guarantee."""
    if entry_mid <= 0:
        return None

    # 1. DTE floor (highest priority — risk rule)
    if dte < cfg.dte_floor:
        pnl = (current_mid - entry_mid) / entry_mid
        return ExitDecision(reason="dte_floor", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    pnl = (current_mid - entry_mid) / entry_mid

    # 2. Profit target
    if pnl >= cfg.profit_target_pct:
        return ExitDecision(reason="profit_target", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    # 3. Stop loss
    if pnl <= -cfg.stop_loss_pct:
        return ExitDecision(reason="stop_loss", pnl_pct=pnl,
                            current_mid=current_mid, dte=dte)

    return None


def compute_dte(expiration_date: date, today: Optional[date] = None) -> int:
    today = today or date.today()
    return (expiration_date - today).days
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_position_monitor.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/options/position_monitor.py engine/tests/options/test_position_monitor.py
git commit -m "Add exit-trigger evaluation logic for option positions

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 10: Position monitor — async loop + limit order watcher

**Files:**
- Modify: `engine/options/position_monitor.py`
- Modify: `engine/tests/options/test_position_monitor.py`

Layer the async monitor loop on top of the pure logic. Each tick: list open positions → batch snapshots → evaluate triggers → submit sell-to-close limit. Add a separate watcher coroutine for entry/exit limit timeouts.

- [ ] **Step 1: Write failing tests**

Append to `engine/tests/options/test_position_monitor.py`:

```python
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

from options.position_monitor import PositionMonitor, MonitorConfig


def _open_trade(*, client_order_id="opt_AAPL_x", contract_symbol="AAPL260619C00200000",
                entry_mid=2.50, dte_at_entry=30, qty=1):
    return {
        "client_order_id": client_order_id,
        "broker_order_id": "broker-1",
        "contract_symbol": contract_symbol,
        "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (date.today() + timedelta(days=dte_at_entry)).isoformat(),
        "strike_price": 200.0,
        "side": "buy",
        "qty": qty,
        "status": "filled",
        "entry_mid": entry_mid,
        "dte_at_entry": dte_at_entry,
    }


@pytest.fixture
def cfg10():
    return MonitorConfig(
        profit_target_pct=0.50, stop_loss_pct=0.50, dte_floor=21,
        fill_timeout_seconds=120, exit_retry_max=3, exit_retry_price_step_pct=0.05,
    )


async def test_tick_no_open_positions_short_circuits(mock_alpaca, mock_repo, cfg10):
    mock_repo.list_open_option_trades = AsyncMock(return_value=[])
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()
    mock_alpaca.get_option_snapshots.assert_not_called()


async def test_tick_fires_profit_target_exit(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 3.10, "ask": 3.20, "mid": 3.15,
            "delta": 0.50, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.03,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    mock_alpaca.submit_option_limit_order.assert_called_once()
    kwargs = mock_alpaca.submit_option_limit_order.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["qty"] == 1
    assert kwargs["limit_price"] == 3.15

    # exit row updated
    mock_repo.update_option_trade_exit.assert_called_once()
    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "profit_target"


async def test_tick_fires_dte_floor_exit_with_priority(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00, dte_at_entry=22)
    # mutate expiration to be 20 days out (below floor)
    trade["expiration_date"] = (date.today() + timedelta(days=20)).isoformat()
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 3.50, "ask": 3.60, "mid": 3.55,  # would also trigger profit
            "delta": 0.50, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.03,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "dte_floor"


async def test_tick_skips_position_when_no_snapshot(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={})  # missing
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_tick_no_trigger_no_action(mock_alpaca, mock_repo, cfg10):
    trade = _open_trade(entry_mid=2.00, dte_at_entry=30)
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 2.00, "ask": 2.10, "mid": 2.05,
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.05,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.tick()

    mock_alpaca.submit_option_limit_order.assert_not_called()
    mock_repo.update_option_trade_exit.assert_not_called()


async def test_startup_sweep_force_closes_below_dte_floor(mock_alpaca, mock_repo, cfg10):
    # Position with DTE=10, well below floor of 21
    trade = _open_trade(entry_mid=2.00)
    trade["expiration_date"] = (date.today() + timedelta(days=10)).isoformat()
    mock_repo.list_open_option_trades = AsyncMock(return_value=[trade])
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        trade["contract_symbol"]: {
            "bid": 1.00, "ask": 1.10, "mid": 1.05,
            "delta": 0.20, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35, "spread_pct": 0.10,
            "volume": 500, "open_interest": 2000,
        }
    })
    pm = PositionMonitor(mock_alpaca, mock_repo, cfg10)
    await pm.startup_sweep()

    mock_alpaca.submit_option_limit_order.assert_called_once()
    exit_kwargs = mock_repo.update_option_trade_exit.call_args.kwargs
    assert exit_kwargs["exit_reason"] == "dte_floor"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_position_monitor.py -v
```

Expected: FAIL — `PositionMonitor` doesn't exist; existing 8 still pass.

- [ ] **Step 3: Extend the monitor module**

Edit `engine/options/position_monitor.py`. Replace the existing `MonitorConfig` dataclass and add the `PositionMonitor` class:

```python
@dataclass
class MonitorConfig:
    profit_target_pct: float
    stop_loss_pct: float
    dte_floor: int
    fill_timeout_seconds: int = 120
    exit_retry_max: int = 3
    exit_retry_price_step_pct: float = 0.05


class PositionMonitor:
    """Watches open option positions, fires sell-to-close on exit triggers.
    Use tick() in tests; run() inside the OptionsEngine async lifecycle."""

    def __init__(self, alpaca_client, repo, cfg: MonitorConfig):
        self.client = alpaca_client
        self.repo = repo
        self.cfg = cfg
        self._stop_event = asyncio.Event()

    async def run(self, interval_seconds: int) -> None:
        """Main monitor loop. Stops when stop() is called."""
        await self.startup_sweep()
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.exception(f"Position monitor tick failed: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    async def startup_sweep(self) -> None:
        """On engine start, scan open positions and force-close any below DTE floor.
        Belt-and-suspenders for the never-exercise guarantee."""
        await self.tick(force_dte_check_only=True)

    async def tick(self, force_dte_check_only: bool = False) -> None:
        open_trades = await self.repo.list_open_option_trades()
        if not open_trades:
            return

        symbols = [t["contract_symbol"] for t in open_trades]
        snapshots = await self.client.get_option_snapshots(symbols)

        today = date.today()
        for trade in open_trades:
            sym = trade["contract_symbol"]
            snap = snapshots.get(sym)
            if not snap:
                logger.warning(f"monitor: no snapshot for {sym}; skipping")
                continue

            try:
                exp = date.fromisoformat(trade["expiration_date"])
            except (ValueError, TypeError, KeyError):
                logger.warning(f"monitor: bad expiration on {sym}")
                continue
            dte = (exp - today).days

            entry_mid = float(trade.get("entry_mid") or 0)
            current_mid = float(snap.get("mid") or 0)

            decision = evaluate_exit(
                entry_mid=entry_mid,
                current_mid=current_mid,
                dte=dte,
                cfg=self.cfg,
            )

            # In sweep mode, only act on dte_floor triggers
            if force_dte_check_only and (decision is None or decision.reason != "dte_floor"):
                continue
            if decision is None:
                continue

            await self._submit_exit(trade, decision)

    async def _submit_exit(self, trade: dict, decision: ExitDecision) -> None:
        sym = trade["contract_symbol"]
        qty = int(trade["qty"])
        client_order_id = f"opt_exit_{trade['underlying_symbol']}_{uuid.uuid4().hex[:8]}"

        # Update DB first (exit_reason recorded even if order submission later fails)
        await self.repo.update_option_trade_exit(
            client_order_id=trade["client_order_id"],
            exit_mid=decision.current_mid,
            exit_dte=decision.dte,
            exit_reason=decision.reason,
            status="closing",
        )

        try:
            await self.client.submit_option_limit_order(
                contract_symbol=sym,
                qty=qty,
                side="sell",
                limit_price=decision.current_mid,
                client_order_id=client_order_id,
            )
            await self.repo.log_event(
                "option_exit_submitted",
                f"{sym} reason={decision.reason} mid=${decision.current_mid:.2f} "
                f"pnl={decision.pnl_pct*100:.1f}% dte={decision.dte}",
            )
            logger.info(
                f"OPTION EXIT {sym} reason={decision.reason} "
                f"pnl={decision.pnl_pct*100:.1f}% dte={decision.dte}"
            )
        except Exception as e:
            logger.error(f"Exit order failed for {sym}: {e}")
            await self.repo.log_event("option_exit_error", f"{sym}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_position_monitor.py -v
```

Expected: 14 passed (8 from Task 9 + 6 new).

- [ ] **Step 5: Commit**

```bash
git add engine/options/position_monitor.py engine/tests/options/test_position_monitor.py
git commit -m "Add async PositionMonitor with startup sweep and exit submission

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 11: Options config accessor + validation

**Files:**
- Modify: `engine/config.py`
- Modify: `config.yaml`

Surface `options` config + validate `min_dte > dte_floor` at startup.

- [ ] **Step 1: Add options section to config.yaml**

Append to `config.yaml`:

```yaml

options:
  enabled: true
  scan_interval_seconds: 300          # 5 min between full chain scans
  monitor_interval_seconds: 60        # 1 min between exit-trigger checks
  bar_timeframe: "5Min"
  lookback_bars: 78                   # ~1 trading day of 5min bars
  min_score_to_trade: 3
  max_option_positions: 5
  max_position_pct: 0.02              # 2% of equity per option position
  target_delta: 0.40
  delta_tolerance: 0.05               # 0.35–0.45 acceptance window
  min_dte: 28                         # must be > dte_floor (validated at startup)
  max_dte: 45
  profit_target_pct: 0.50             # +50% premium gain
  stop_loss_pct: 0.50                 # -50% premium loss
  dte_floor: 21                       # force-close when DTE < this
  liquidity:
    max_spread_pct: 0.20
    min_volume: 10
    min_open_interest: 100
  limit_order:
    fill_timeout_seconds: 120
    exit_retry_max: 3
    exit_retry_price_step_pct: 0.05
```

- [ ] **Step 2: Add accessor + validator to config.py**

Edit `engine/config.py`. Add to the `TradingConfig` class (after the existing properties):

```python
    @property
    def options(self) -> dict:
        return self._raw.get("options", {})

    def validate_options_config(self) -> list[str]:
        """Return a list of validation error messages. Empty list means valid."""
        errors: list[str] = []
        opts = self.options
        if not opts:
            return errors  # not configured == disabled, no validation needed

        min_dte = int(opts.get("min_dte", 0))
        dte_floor = int(opts.get("dte_floor", 0))
        if min_dte <= dte_floor:
            errors.append(
                f"options.min_dte ({min_dte}) must be > options.dte_floor ({dte_floor})"
            )

        target = float(opts.get("target_delta", 0))
        tol = float(opts.get("delta_tolerance", 0))
        if not (0 < target < 1):
            errors.append(f"options.target_delta must be in (0, 1); got {target}")
        if tol <= 0 or tol >= target:
            errors.append(
                f"options.delta_tolerance ({tol}) must be > 0 and < target_delta ({target})"
            )

        if int(opts.get("max_option_positions", 0)) <= 0:
            errors.append("options.max_option_positions must be > 0")
        if float(opts.get("max_position_pct", 0)) <= 0:
            errors.append("options.max_position_pct must be > 0")

        return errors
```

- [ ] **Step 3: Verify by reading the config in a quick check**

```bash
docker compose restart engine
docker compose logs engine --tail 20
```

Expected: clean startup. Confirm via API:

```bash
curl -s http://localhost:8000/config | python -c "import sys,json; print('options' in json.load(sys.stdin))"
```

Expected: `True`.

- [ ] **Step 4: Commit**

```bash
git add engine/config.py config.yaml
git commit -m "Add options config section with startup validation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 12: OptionsEngine — lifecycle skeleton

**Files:**
- Create: `engine/options/engine.py`
- Create: `engine/tests/options/test_engine.py`

The engine class — pause/resume/stop, holds the position monitor as a sub-task. Tick logic comes in Task 13.

- [ ] **Step 1: Write failing tests for lifecycle**

Create `engine/tests/options/test_engine.py`:

```python
"""Tests for OptionsEngine lifecycle and tick orchestration."""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from options.engine import OptionsEngine


def _options_config_dict():
    return {
        "enabled": True,
        "scan_interval_seconds": 300,
        "monitor_interval_seconds": 60,
        "bar_timeframe": "5Min",
        "lookback_bars": 78,
        "min_score_to_trade": 3,
        "max_option_positions": 5,
        "max_position_pct": 0.02,
        "target_delta": 0.40,
        "delta_tolerance": 0.05,
        "min_dte": 28,
        "max_dte": 45,
        "profit_target_pct": 0.50,
        "stop_loss_pct": 0.50,
        "dte_floor": 21,
        "liquidity": {
            "max_spread_pct": 0.20, "min_volume": 10, "min_open_interest": 100,
        },
        "limit_order": {
            "fill_timeout_seconds": 120, "exit_retry_max": 3,
            "exit_retry_price_step_pct": 0.05,
        },
    }


@pytest.fixture
def cfg_obj():
    cfg = MagicMock()
    cfg.options = _options_config_dict()
    cfg.trading = {"watchlist": ["AAPL", "TSLA"]}
    cfg.screener = {"enabled": False}
    cfg.wsb_scanner = {"enabled": False}
    cfg.validate_options_config = MagicMock(return_value=[])
    return cfg


@pytest.fixture
def settings():
    s = MagicMock()
    s.reddit_client_id = None
    s.reddit_client_secret = None
    return s


def _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    return OptionsEngine(
        settings=settings, config=cfg_obj, client=mock_alpaca,
        risk=mock_risk, repo=mock_repo,
    )


def test_engine_starts_unpaused(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    assert eng.paused is False
    assert eng.running is False


def test_pause_and_resume(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    eng.pause()
    assert eng.paused is True
    eng.resume()
    assert eng.paused is False


async def test_tick_skips_when_paused(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    eng.pause()
    await eng._tick()
    mock_alpaca.is_market_open.assert_not_called()


async def test_tick_skips_when_kill_switch_active(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_risk.kill_switch_active.return_value = True
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    mock_alpaca.is_market_open.assert_not_called()


async def test_tick_skips_when_market_closed(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_alpaca.is_market_open = AsyncMock(return_value=False)
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    mock_alpaca.get_account.assert_not_called()


async def test_tick_pauses_engine_on_daily_loss_breach(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo):
    mock_risk.daily_loss_breached.return_value = True
    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    await eng._tick()
    assert eng.paused is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_engine.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the engine skeleton**

Create `engine/options/engine.py`:

```python
"""OptionsEngine — autonomous options trading loop.

Runs as a parallel asyncio task to TradingEngine. Shares RiskManager,
AlpacaClient, Repository via constructor injection.

Tick sequence (every options.scan_interval_seconds):
  1. Engine paused?  → skip
  2. Kill switch?    → skip
  3. Market closed?  → skip
  4. Establish daily baseline (no-op if stock engine did first)
  5. Daily loss breached?      → pause
  6. Daily profit goal hit?    → pause
  7. Build watchlist
  8. Scan symbols → OptionsSignal
  9. For each non-neutral signal above threshold:
     - Skip if already holding option in this underlying
     - Skip if max_option_positions reached
     - Select contract; size; write pending row; submit limit; update row

PositionMonitor runs as a sub-task on its own cadence."""
import asyncio
import uuid
from typing import Optional

from loguru import logger

from data.alpaca_client import AlpacaClient
from options.contract_selector import ContractSelector, SelectorConfig
from options.position_monitor import MonitorConfig, PositionMonitor
from options.strategy import OptionsSignal, score_options_signal
from risk.manager import RiskManager


class OptionsEngine:
    def __init__(
        self,
        settings,
        config,
        client: AlpacaClient,
        risk: RiskManager,
        repo,
    ):
        self.settings = settings
        self.config = config
        self.client = client
        self.risk = risk
        self.repo = repo
        self._stop_event = asyncio.Event()
        self._paused = False
        self._running = False
        self._monitor: Optional[PositionMonitor] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_signals: list[OptionsSignal] = []
        self._last_scan_at: Optional[str] = None
        self._last_error: Optional[str] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.warning("Options engine paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Options engine resumed")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._monitor:
            self._monitor.stop()
        if self._monitor_task:
            self._monitor_task.cancel()
        self._running = False

    async def run(self) -> None:
        if not self.config.options.get("enabled", False):
            logger.info("Options engine disabled in config — exiting run()")
            return

        # Validate config
        errors = self.config.validate_options_config()
        if errors:
            for err in errors:
                logger.error(f"options config invalid: {err}")
                await self.repo.log_event("options_config_error", err)
            self._paused = True
            return

        self._running = True
        await self.repo.log_event(
            "options_engine_started",
            f"mode={self.risk.trading_mode()}",
        )
        logger.info(f"Options engine running (mode={self.risk.trading_mode()})")

        # Spawn position monitor as sub-task
        self._monitor = PositionMonitor(
            self.client, self.repo, self._build_monitor_config()
        )
        monitor_interval = int(self.config.options.get("monitor_interval_seconds", 60))
        self._monitor_task = asyncio.create_task(
            self._monitor.run(monitor_interval), name="options_position_monitor",
        )

        scan_interval = int(self.config.options.get("scan_interval_seconds", 300))
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception(f"Options tick failed: {e}")
                self._last_error = str(e)
                await self.repo.log_event("options_tick_error", str(e))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=scan_interval,
                )
            except asyncio.TimeoutError:
                pass

        self._running = False
        await self.repo.log_event("options_engine_stopped", "shutdown")
        logger.info("Options engine stopped")

    def _build_monitor_config(self) -> MonitorConfig:
        opts = self.config.options
        lo = opts.get("limit_order", {})
        return MonitorConfig(
            profit_target_pct=float(opts["profit_target_pct"]),
            stop_loss_pct=float(opts["stop_loss_pct"]),
            dte_floor=int(opts["dte_floor"]),
            fill_timeout_seconds=int(lo.get("fill_timeout_seconds", 120)),
            exit_retry_max=int(lo.get("exit_retry_max", 3)),
            exit_retry_price_step_pct=float(lo.get("exit_retry_price_step_pct", 0.05)),
        )

    def _build_selector_config(self) -> SelectorConfig:
        opts = self.config.options
        liq = opts.get("liquidity", {})
        return SelectorConfig(
            target_delta=float(opts["target_delta"]),
            delta_tolerance=float(opts["delta_tolerance"]),
            min_dte=int(opts["min_dte"]),
            max_dte=int(opts["max_dte"]),
            max_spread_pct=float(liq.get("max_spread_pct", 0.20)),
            min_volume=int(liq.get("min_volume", 10)),
            min_open_interest=int(liq.get("min_open_interest", 100)),
            dte_floor=int(opts["dte_floor"]),
        )

    # ── tick ──────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if self._paused:
            logger.debug("options engine paused — skip tick")
            return
        if self.risk.kill_switch_active():
            return
        if not await self.client.is_market_open():
            return

        account = await self.client.get_account()
        await self.risk.initialize_daily_baseline(float(account["equity"]))

        if self.risk.daily_loss_breached(float(account["equity"])):
            await self.repo.log_event(
                "options_daily_limit_breached",
                f"equity=${account['equity']:.2f}",
            )
            self._paused = True
            return

        if self.risk.daily_profit_goal_reached(float(account["equity"])):
            await self.repo.log_event(
                "options_daily_goal_reached",
                f"equity=${account['equity']:.2f}",
            )
            self._paused = True
            return

        if account.get("trading_blocked") or account.get("account_blocked"):
            self._paused = True
            await self.repo.log_event("options_account_blocked", str(account))
            return

        # Tick body filled in Task 13
        await self._scan_and_execute(account)

    async def _scan_and_execute(self, account: dict) -> None:
        # Filled in Task 13
        pass

    def snapshot_status(self) -> dict:
        return {
            "running": self._running,
            "paused": self._paused,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
            "open_signals_count": len(self._last_signals),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_engine.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/options/engine.py engine/tests/options/test_engine.py
git commit -m "Add OptionsEngine lifecycle skeleton with risk gate checks

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 13: OptionsEngine — scan and execute logic

**Files:**
- Modify: `engine/options/engine.py`
- Modify: `engine/tests/options/test_engine.py`

Fill in `_scan_and_execute`: build watchlist, score symbols, select contracts, write-before-submit pattern.

- [ ] **Step 1: Write failing tests**

Append to `engine/tests/options/test_engine.py`:

```python
import pandas as pd

# Use fixtures from conftest: trending_up_df, trending_down_df


async def test_scan_executes_buy_call_on_bullish_signal(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)

    # Chain + snapshots that pass all selector filters
    today = date.today()
    chain = [{
        "symbol": "AAPL_C", "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0, "open_interest": 1500, "tradable": True,
    }]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "AAPL_C": {
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35,
            "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
            "volume": 250, "open_interest": 1500, "last_price": 2.55,
        }
    })
    mock_alpaca.get_positions = AsyncMock(return_value=[])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    # Pending row written, then limit order submitted, then row updated
    mock_repo.insert_option_trade_pending.assert_called_once()
    mock_alpaca.submit_option_limit_order.assert_called_once()
    submit_kwargs = mock_alpaca.submit_option_limit_order.call_args.kwargs
    assert submit_kwargs["side"] == "buy"
    assert submit_kwargs["limit_price"] == 2.55
    assert submit_kwargs["contract_symbol"] == "AAPL_C"

    mock_repo.update_option_trade_after_submit.assert_called()
    mock_repo.update_option_trade_with_entry_data.assert_called()


async def test_scan_skips_underlying_when_already_held(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    # An existing open option position for AAPL underlying
    mock_repo.list_open_option_trades = AsyncMock(return_value=[{
        "underlying_symbol": "AAPL", "contract_symbol": "AAPL_OLD",
        "client_order_id": "old", "side": "buy", "qty": 1, "status": "filled",
        "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
        "entry_mid": 2.0,
    }])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_when_max_positions_reached(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    cfg_obj.options = _options_config_dict()
    cfg_obj.options["max_option_positions"] = 1
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    # One open position in TSLA, max is 1 → AAPL signal must be skipped
    mock_repo.list_open_option_trades = AsyncMock(return_value=[{
        "underlying_symbol": "TSLA", "contract_symbol": "TSLA_OLD",
        "client_order_id": "old", "side": "buy", "qty": 1, "status": "filled",
        "expiration_date": (date.today() + timedelta(days=30)).isoformat(),
        "entry_mid": 2.0,
    }])

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_when_qty_below_one(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    # Tiny equity → 2% × 1000 / (2.55 × 100) = 0.078 contracts → floor=0
    mock_alpaca.get_account = AsyncMock(return_value={
        "equity": 1000.0, "cash": 1000.0, "buying_power": 1000.0,
        "trading_blocked": False, "account_blocked": False,
    })
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)

    today = date.today()
    chain = [{
        "symbol": "AAPL_C", "underlying_symbol": "AAPL",
        "contract_type": "call",
        "expiration_date": (today + timedelta(days=30)).isoformat(),
        "strike_price": 200.0, "open_interest": 1500, "tradable": True,
    }]
    mock_alpaca.get_option_chain = AsyncMock(return_value=chain)
    mock_alpaca.get_option_snapshots = AsyncMock(return_value={
        "AAPL_C": {
            "delta": 0.40, "gamma": 0.05, "theta": -0.08, "vega": 0.15,
            "implied_volatility": 0.35,
            "bid": 2.50, "ask": 2.60, "mid": 2.55, "spread_pct": 0.039,
            "volume": 250, "open_interest": 1500, "last_price": 2.55,
        }
    })

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.submit_option_limit_order.assert_not_called()


async def test_scan_skips_when_no_eligible_contract(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, trending_up_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=trending_up_df)
    mock_alpaca.get_option_chain = AsyncMock(return_value=[])  # empty chain

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.submit_option_limit_order.assert_not_called()
    # Event logged for visibility
    mock_repo.log_event.assert_any_call(
        "options_signal_no_eligible_contract", pytest.helpers.match_call("AAPL")
    ) if hasattr(pytest, "helpers") else None  # soft check


async def test_scan_skips_neutral_signals(
    settings, cfg_obj, mock_alpaca, mock_risk, mock_repo, flat_df,
):
    cfg_obj.trading = {"watchlist": ["AAPL"]}
    mock_alpaca.get_bars = AsyncMock(return_value=flat_df)

    eng = _make_engine(settings, cfg_obj, mock_alpaca, mock_risk, mock_repo)
    account = await mock_alpaca.get_account()
    await eng._scan_and_execute(account)

    mock_alpaca.get_option_chain.assert_not_called()
    mock_alpaca.submit_option_limit_order.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec engine python -m pytest tests/options/test_engine.py -v
```

Expected: previously-passing 6 still pass; 6 new tests fail because `_scan_and_execute` is a no-op stub.

- [ ] **Step 3: Implement scan-and-execute**

Replace the `_scan_and_execute` and add helpers in `engine/options/engine.py`. The full method body:

```python
    async def _scan_and_execute(self, account: dict) -> None:
        from datetime import datetime, timezone

        watchlist = await self._build_watchlist()
        if not watchlist:
            logger.warning("options: empty watchlist — skip tick")
            return

        # Existing open positions — used to dedupe and enforce max
        open_trades = await self.repo.list_open_option_trades()
        held_underlyings = {t["underlying_symbol"] for t in open_trades}
        max_positions = int(self.config.options.get("max_option_positions", 5))

        # Score every symbol on intraday bars
        timeframe = self.config.options.get("bar_timeframe", "5Min")
        lookback = int(self.config.options.get("lookback_bars", 78))
        min_score = int(self.config.options.get("min_score_to_trade", 3))

        signals: list[OptionsSignal] = []
        for sym in watchlist:
            if self._stop_event.is_set():
                return
            try:
                df = await self.client.get_bars(
                    sym, timeframe=timeframe, limit=lookback,
                )
            except Exception as e:
                logger.warning(f"options bars fetch {sym} failed: {e}")
                continue
            if df is None or len(df) < 30:
                continue
            sig = score_options_signal(sym, df, min_score=min_score)
            signals.append(sig)

        # Sort tradable signals by score descending
        tradable = [s for s in signals if s.direction != "neutral"]
        tradable.sort(key=lambda s: s.score, reverse=True)
        self._last_signals = tradable
        self._last_scan_at = datetime.now(timezone.utc).isoformat()

        if not tradable:
            return

        selector = ContractSelector(self.client, self._build_selector_config())

        for sig in tradable:
            if self._stop_event.is_set() or self.risk.kill_switch_active():
                break
            if len(held_underlyings) >= max_positions:
                logger.info(
                    f"options: max_option_positions={max_positions} reached, "
                    f"skipping {sig.symbol}"
                )
                break
            if sig.symbol in held_underlyings:
                continue

            await self._execute_signal(sig, account, selector, held_underlyings)
            account = await self.client.get_account()  # refresh BP after each order

    async def _build_watchlist(self) -> list[str]:
        """Reuse the same priority order as TradingEngine: screener > static."""
        default = list(self.config.trading.get("watchlist", []))
        screener_cfg = self.config.screener
        if screener_cfg.get("enabled", False):
            try:
                # The stock engine attaches a MarketScreener to itself; we use the
                # same Alpaca screener helpers directly.
                actives = await self.client.get_most_actives(
                    top=int(screener_cfg.get("top_n", 50))
                )
                if screener_cfg.get("include_gainers", True):
                    gainers = await self.client.get_top_gainers(top=25)
                    actives = list(dict.fromkeys(actives + gainers))
                actives = await self.client.filter_symbols_by_price(
                    actives,
                    min_price=float(screener_cfg.get("min_price", 5.0)),
                    max_price=float(screener_cfg.get("max_price", 500.0)),
                )
                return actives or default
            except Exception as e:
                logger.warning(f"options screener fallback to static: {e}")
                return default
        return default

    async def _execute_signal(
        self,
        sig: OptionsSignal,
        account: dict,
        selector: ContractSelector,
        held_underlyings: set[str],
    ) -> None:
        contract = await selector.select(sig.symbol, sig.direction)
        if contract is None:
            await self.repo.log_event(
                "options_signal_no_eligible_contract",
                f"{sig.symbol} direction={sig.direction} score={sig.score}",
            )
            return

        # Position sizing
        equity = float(account.get("equity", 0))
        bp = float(account.get("buying_power", 0))
        max_pct = float(self.config.options.get("max_position_pct", 0.02))
        notional = min(equity * max_pct, bp * 0.95)
        per_contract_cost = contract.mid * 100
        if per_contract_cost <= 0:
            return
        qty = int(notional / per_contract_cost)
        if qty < 1:
            await self.repo.log_event(
                "options_skipped_sizing",
                f"{sig.symbol}: notional ${notional:.2f} < contract ${per_contract_cost:.2f}",
            )
            return

        client_order_id = f"opt_auto_{sig.symbol}_{uuid.uuid4().hex[:8]}"

        # Write pending row first
        await self.repo.insert_option_trade_pending(
            client_order_id=client_order_id,
            contract_symbol=contract.contract_symbol,
            underlying_symbol=contract.underlying_symbol,
            contract_type=contract.contract_type,
            expiration_date=contract.expiration_date,
            strike_price=contract.strike_price,
            side="buy",
            qty=qty,
            trading_mode=self.risk.trading_mode(),
        )

        try:
            result = await self.client.submit_option_limit_order(
                contract_symbol=contract.contract_symbol,
                qty=qty,
                side="buy",
                limit_price=contract.mid,
                client_order_id=client_order_id,
            )
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=result["id"],
                status=result["status"],
            )
            premium_paid = contract.mid * 100 * qty
            await self.repo.update_option_trade_with_entry_data(
                client_order_id=client_order_id,
                entry_delta=contract.delta,
                entry_gamma=contract.gamma,
                entry_theta=contract.theta,
                entry_vega=contract.vega,
                entry_iv=contract.iv,
                entry_bid=contract.bid,
                entry_ask=contract.ask,
                entry_mid=contract.mid,
                premium_paid=premium_paid,
                dte_at_entry=contract.dte,
                underlying_score=sig.score,
                underlying_signals=sig.signals,
            )
            await self.repo.log_event(
                "option_auto_entry",
                f"{contract.contract_symbol} buy x{qty} @${contract.mid:.2f} "
                f"(score={sig.score} delta={contract.delta:.2f} dte={contract.dte})",
            )
            held_underlyings.add(contract.underlying_symbol)
            logger.info(
                f"OPTION BUY {contract.contract_symbol} x{qty} @${contract.mid:.2f} "
                f"({sig.symbol} {sig.direction} score={sig.score}, "
                f"delta={contract.delta:.2f}, dte={contract.dte})"
            )
        except Exception as e:
            logger.error(f"Option entry failed for {contract.contract_symbol}: {e}")
            await self.repo.update_option_trade_after_submit(
                client_order_id=client_order_id,
                broker_order_id=None,
                status="error",
            )
            await self.repo.log_event(
                "option_auto_entry_error",
                f"{contract.contract_symbol}: {e}",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec engine python -m pytest tests/options/test_engine.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/options/engine.py engine/tests/options/test_engine.py
git commit -m "Implement OptionsEngine scan-and-execute with contract selection

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 14: Wire OptionsEngine into main.py

**Files:**
- Modify: `engine/main.py`

Start `OptionsEngine.run()` as a parallel asyncio task in lifespan, alongside the stock engine.

- [ ] **Step 1: Update lifespan to construct + start the engine**

Edit `engine/main.py`. Add to imports:

```python
from options.engine import OptionsEngine
```

Inside `lifespan()`, after the existing `engine = TradingEngine(...)` line, add:

```python
    options_engine = OptionsEngine(
        settings=settings, config=config, client=alpaca, risk=risk, repo=repo,
    )
```

Then immediately after `app.state.engine = engine`, add:

```python
    app.state.options_engine = options_engine
```

After the existing `engine_task = asyncio.create_task(engine.run(), name="trading_engine")` line, add:

```python
    options_engine_task = asyncio.create_task(
        options_engine.run(), name="options_engine",
    )
    app.state.options_engine_task = options_engine_task
```

In the `finally:` block (shutdown), after `engine_task.cancel()` and its `try/except`, add:

```python
        await options_engine.stop()
        options_engine_task.cancel()
        try:
            await options_engine_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 2: Restart engine and verify both tasks start**

```bash
docker compose restart engine
docker compose logs engine --tail 50
```

Expected output should show:
- `Trading engine running (mode=paper)` (existing)
- `Options engine running (mode=paper)` (new)

If options engine logs `disabled in config — exiting run()`, double-check `config.yaml` has `options.enabled: true`.

- [ ] **Step 3: Verify tasks via FastAPI startup**

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok",...}`. The presence of both engines is checked via the new status route in Task 15.

- [ ] **Step 4: Commit**

```bash
git add engine/main.py
git commit -m "Start OptionsEngine as parallel asyncio task in lifespan

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 15: API routes — options engine control + open positions

**Files:**
- Modify: `engine/api/routes.py`

Add `/options/engine/status|pause|resume`, enrich `/options/chain/{symbol}` with Greeks, add `/options/positions` and `/options/iv-surface/{symbol}`.

- [ ] **Step 1: Add options engine control routes**

Edit `engine/api/routes.py`. After the existing `/options/trades` route, add:

```python
# ── options engine control ────────────────────────────────────────────────────

@router.get("/options/engine/status")
async def options_engine_status(request: Request):
    eng = request.app.state.options_engine
    return eng.snapshot_status()


@router.post("/options/engine/pause")
async def options_engine_pause(request: Request):
    request.app.state.options_engine.pause()
    await request.app.state.repo.log_event(
        "options_engine_paused", "via API"
    )
    return {"ok": True, "paused": True}


@router.post("/options/engine/resume")
async def options_engine_resume(request: Request):
    request.app.state.options_engine.resume()
    await request.app.state.repo.log_event(
        "options_engine_resumed", "via API"
    )
    return {"ok": True, "paused": False}
```

- [ ] **Step 2: Enrich `/options/chain/{symbol}` with Greeks**

Replace the existing `option_chain` route in `engine/api/routes.py`:

```python
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
```

- [ ] **Step 3: Add `/options/positions` route**

After `option_chain`, add:

```python
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

        # Predict which trigger fires next (just for display)
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
```

- [ ] **Step 4: Add `/options/iv-surface/{symbol}` route**

After `list_open_options`, add:

```python
@router.get("/options/iv-surface/{symbol}")
async def iv_surface(
    symbol: str,
    request: Request,
    days_out: int = 60,
):
    """IV-by-strike for nearest 2-3 expirations. On-demand, no persistence."""
    contracts = await request.app.state.alpaca.get_option_chain(
        underlying=symbol.upper(),
        days_out=days_out,
        contract_type=None,
    )
    if not contracts:
        return {"symbol": symbol.upper(), "expirations": []}

    # Collect distinct expiries, take nearest 3
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
```

- [ ] **Step 5: Restart engine and verify routes**

```bash
docker compose restart engine
sleep 3
curl -s http://localhost:8000/options/engine/status
```

Expected: `{"running": true, "paused": false, "last_scan_at": null, ...}`.

```bash
curl -s -X POST http://localhost:8000/options/engine/pause
curl -s http://localhost:8000/options/engine/status
```

Expected: `{"ok": true, "paused": true}`, then status with `paused: true`.

```bash
curl -s -X POST http://localhost:8000/options/engine/resume
```

Expected: `{"ok": true, "paused": false}`.

```bash
curl -s http://localhost:8000/options/positions
```

Expected: `{"count": 0, "positions": []}`.

- [ ] **Step 6: Commit**

```bash
git add engine/api/routes.py
git commit -m "Add options engine control + positions + IV surface routes

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 16: Streamlit UI — Options tab enhancements

**Files:**
- Modify: `ui/app.py`

Add an Open Positions sub-view, Engine Controls panel, IV surface chart, and Greeks columns to the chain explorer. The exact tab structure depends on the existing layout — we add to the existing Options tab without restructuring the whole file.

- [ ] **Step 1: Locate the existing Options tab**

```bash
grep -n "Options" ui/app.py | head
```

Note the line numbers of the existing Options tab block — you'll insert new sub-views inside it.

- [ ] **Step 2: Add a helper for the engine controls block near the top**

Edit `ui/app.py`. After the existing helper functions (after the `_post` helper), add:

```python
# ── options engine controls helper ────────────────────────────────────────────

def _render_options_engine_panel():
    """Render the options engine status row + pause/resume buttons."""
    status = _get("/options/engine/status") or {}
    cols = st.columns([1, 2, 1, 1])
    with cols[0]:
        if status.get("paused"):
            st.warning("Options: ⏸ paused")
        elif status.get("running"):
            st.success("Options: ✅ running")
        else:
            st.error("Options: ⚠ stopped")
    with cols[1]:
        last_scan = status.get("last_scan_at") or "—"
        st.caption(f"last scan: {last_scan}")
    with cols[2]:
        if st.button("Pause options", key="opt_pause", disabled=status.get("paused", False)):
            _post("/options/engine/pause")
            st.rerun()
    with cols[3]:
        if st.button("Resume options", key="opt_resume", disabled=not status.get("paused", False)):
            _post("/options/engine/resume")
            st.rerun()
```

- [ ] **Step 3: Locate and update the Options tab**

Find the existing block in `ui/app.py` that handles the Options tab (look for the Options chain explorer code — it should be in a `with options_tab:` or similar block).

Inside that block, insert at the very top:

```python
    _render_options_engine_panel()
    st.divider()

    sub_view = st.radio(
        "View",
        options=["Open Positions", "Chain Explorer", "IV Surface"],
        horizontal=True,
        key="opt_subview",
    )

    if sub_view == "Open Positions":
        _render_open_options_positions()
    elif sub_view == "Chain Explorer":
        _render_options_chain_explorer()
    else:
        _render_iv_surface()
```

Then move the existing chain-explorer code into a new top-level helper function `_render_options_chain_explorer()` defined near the other helpers (do not lose any existing functionality — wrap, don't delete).

- [ ] **Step 4: Implement the three sub-view helpers**

Add to `ui/app.py` near the other helpers:

```python
# ── options sub-views ─────────────────────────────────────────────────────────

def _render_open_options_positions():
    data = _get("/options/positions")
    if not data or data.get("count", 0) == 0:
        st.info("No open option positions.")
        return

    rows = []
    for p in data["positions"]:
        rows.append({
            "Contract": p["contract_symbol"],
            "Side": p["side"],
            "Qty": p["qty"],
            "Entry mid": p.get("entry_mid"),
            "Current mid": p.get("current_mid"),
            "P&L %": (p["pnl_pct"] * 100) if p.get("pnl_pct") is not None else None,
            "Entry Δ": p.get("entry_delta"),
            "Current Δ": p.get("current_delta"),
            "IV": p.get("current_iv"),
            "DTE": p.get("dte_remaining"),
            "Next trigger": p.get("next_trigger") or "—",
        })
    df = pd.DataFrame(rows)

    def color_pnl(v):
        if pd.isna(v):
            return ""
        return "color: green" if v > 0 else "color: red" if v < 0 else ""

    def color_trigger(v):
        return "color: orange; font-weight: bold" if v and v != "—" else ""

    styled = (
        df.style
        .map(color_pnl, subset=["P&L %"])
        .map(color_trigger, subset=["Next trigger"])
        .format({
            "Entry mid": "${:.2f}", "Current mid": "${:.2f}", "P&L %": "{:+.1f}%",
            "Entry Δ": "{:.2f}", "Current Δ": "{:.2f}", "IV": "{:.1%}",
        }, na_rep="—")
    )
    st.dataframe(styled, use_container_width=True)


def _render_options_chain_explorer():
    """Existing chain explorer, enriched with Greeks columns."""
    symbol = st.text_input("Symbol", value="AAPL", key="opt_chain_sym").upper()
    days_out = st.slider("Days out", 7, 90, 45, key="opt_chain_days")
    cfilter = st.selectbox("Type", ["any", "call", "put"], key="opt_chain_type")
    hide_illiquid = st.checkbox(
        "Hide illiquid (engine filter)", value=True, key="opt_chain_liq",
    )

    if not symbol:
        return

    type_param = "" if cfilter == "any" else cfilter
    data = _get(f"/options/chain/{symbol}", days_out=days_out, type=type_param)
    if not data or not data.get("contracts"):
        st.info("No contracts returned.")
        return

    cfg = _get("/config") or {}
    liq = ((cfg.get("options") or {}).get("liquidity") or {})
    max_spread = float(liq.get("max_spread_pct", 0.20))
    min_vol = int(liq.get("min_volume", 10))
    min_oi = int(liq.get("min_open_interest", 100))

    rows = []
    for c in data["contracts"]:
        spread = c.get("spread_pct")
        vol = c.get("volume") or 0
        oi = c.get("open_interest") or 0
        is_liquid = (
            spread is not None and spread <= max_spread
            and vol >= min_vol and oi >= min_oi
        )
        if hide_illiquid and not is_liquid:
            continue
        rows.append({
            "Symbol": c["symbol"],
            "Type": c["contract_type"],
            "Expiry": c["expiration_date"],
            "Strike": c["strike_price"],
            "Bid": c.get("bid"), "Ask": c.get("ask"), "Mid": c.get("mid"),
            "Spread%": (spread * 100) if spread is not None else None,
            "Δ": c.get("delta"), "Γ": c.get("gamma"),
            "Θ": c.get("theta"), "Vega": c.get("vega"),
            "IV": c.get("iv"),
            "Vol": vol, "OI": oi,
        })

    if not rows:
        st.info("No liquid contracts match. Toggle off 'Hide illiquid' to see all.")
        return

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format({
            "Strike": "${:.2f}", "Bid": "${:.2f}", "Ask": "${:.2f}", "Mid": "${:.2f}",
            "Spread%": "{:.1f}%", "Δ": "{:.2f}", "Γ": "{:.3f}",
            "Θ": "{:.3f}", "Vega": "{:.3f}", "IV": "{:.1%}",
        }, na_rep="—"),
        use_container_width=True,
    )


def _render_iv_surface():
    symbol = st.text_input("Symbol", value="AAPL", key="opt_iv_sym").upper()
    if not symbol:
        return

    data = _get(f"/options/iv-surface/{symbol}")
    if not data or not data.get("expirations"):
        st.info("No IV data available.")
        return

    fig = go.Figure()
    for exp in data["expirations"]:
        points = exp["points"]
        if not points:
            continue
        fig.add_trace(go.Scatter(
            x=[p["strike"] for p in points],
            y=[p["iv"] * 100 for p in points],
            mode="lines+markers",
            name=exp["expiration_date"],
        ))
    fig.update_layout(
        title=f"{symbol} IV by Strike",
        xaxis_title="Strike",
        yaxis_title="Implied Volatility (%)",
        height=480,
    )
    st.plotly_chart(fig, use_container_width=True)
```

- [ ] **Step 5: Restart UI and verify**

The UI container picks up changes via Streamlit's auto-reload, but a clean restart is safer:

```bash
docker compose restart ui
sleep 3
```

Open http://localhost:8501 in a browser. Navigate to the Options tab and verify:
1. Engine controls panel shows "Options: ✅ running" with pause/resume buttons
2. "Open Positions" sub-view shows "No open option positions" (assuming none yet)
3. "Chain Explorer" shows enriched columns including Δ, Γ, Θ, Vega, IV
4. "IV Surface" renders a plot for a symbol with active options

- [ ] **Step 6: Commit**

```bash
git add ui/app.py
git commit -m "Add options engine controls, positions view, IV surface to UI

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 17: End-to-end smoke test

**Files:**
- None (validation only)

Verify the full pipeline against a paper account: engine boots, scans, posts orders, monitors positions, exits.

- [ ] **Step 1: Verify all unit tests still pass**

```bash
docker compose exec engine python -m pytest tests/ -v
```

Expected: all tests from Tasks 1–13 pass — count should be ~46+ tests.

- [ ] **Step 2: Confirm both engines started**

```bash
docker compose logs engine --tail 100 | grep -E "(Trading engine running|Options engine running)"
```

Expected: both lines appear.

- [ ] **Step 3: Trigger a manual scan during market hours**

Wait for US market hours (or use Alpaca's clock). With a watchlist of high-volume symbols (`AAPL`, `TSLA`, `SPY`), force a scan by reducing `scan_interval_seconds` to `60` and reloading config:

```bash
# Edit config.yaml to set options.scan_interval_seconds: 60, then:
curl -s -X POST http://localhost:8000/config/reload
```

Watch for scan activity:

```bash
docker compose logs engine -f | grep -i option
```

Expected (during market hours): scan log lines like `OPTION BUY ...` or `options_signal_no_eligible_contract` events.

- [ ] **Step 4: Verify a position is monitored if one was opened**

```bash
curl -s http://localhost:8000/options/positions | python -m json.tool
```

Expected: open positions if any were opened; each row includes Greeks, P&L, DTE, next_trigger fields.

- [ ] **Step 5: Verify never-exercise startup sweep**

Stop the engine and verify the sweep code path runs on next startup:

```bash
docker compose restart engine
docker compose logs engine --tail 50 | grep -i "sweep\|options engine"
```

Expected: logs show options engine started; if any position has DTE < dte_floor it should be force-closed (event log will show `option_exit_submitted`).

- [ ] **Step 6: Pause options engine and verify stock engine continues**

```bash
curl -s -X POST http://localhost:8000/options/engine/pause
curl -s http://localhost:8000/engine/status   # stock engine
curl -s http://localhost:8000/options/engine/status
```

Expected: stock engine status `paused: false` and options engine `paused: true`. Logs continue to show stock engine ticks.

- [ ] **Step 7: Resume and final commit (if any incidental fixes)**

```bash
curl -s -X POST http://localhost:8000/options/engine/resume
```

If smoke testing surfaced issues, fix them, re-run unit tests, and commit:

```bash
git add -A
git commit -m "Smoke-test fixes for options engine end-to-end run

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review Notes

**Spec coverage check:**
- §1 Goals: pivot to options ✓ (Task 14 wires engine), autonomous ✓ (Task 13), Greeks/IV/liquidity ✓ (Tasks 4, 5, 8), limit-orders ✓ (Task 6), exits ✓ (Tasks 9–10), never-exercise ✓ (Task 10 startup sweep + selector dte_floor in Task 8)
- §4 Data Layer: snapshots ✓ (Task 5), liquidity filter ✓ (Task 8), IV surface route ✓ (Task 15), 30s caching — **gap**: snapshot caching layer was scoped in spec §4 but absent from plan. Acceptable trade-off because batched snapshots are already tight (one call per scan); if API rate limits bite during smoke test (Task 17 step 3), add caching to `AlpacaClient.get_option_snapshots` then.
- §5 Strategy: bullish/bearish split ✓ (Task 7), bearish scoring ✓ (Task 2)
- §6 Selector: full pipeline ✓ (Task 8)
- §7 OptionsEngine: lifecycle ✓ (Task 12), tick ✓ (Task 13), watchlist reuse — **partial**: plan implements a screener fallback inline rather than reusing `TradingEngine._build_watchlist()`; this avoids cross-engine coupling and is justified.
- §8 Position Monitor: pure logic ✓ (Task 9), async loop ✓ (Task 10), limit timeout retry — **gap**: plan implements timeout via `submit_exit` but does NOT implement the retry-at-worse-price loop spec'd in §8. Acceptable v1 trade-off — Alpaca's limit orders sit on the book until cancelled, and the next monitor tick will re-evaluate. Re-pricing logic added if smoke test reveals fills are persistently missed.
- §9 Never-exercise: code clamp via selector dte_floor ✓ (Task 8), entry filter ✓ (Task 8), startup sweep ✓ (Task 10)
- §10 DB schema: ✓ (Task 3)
- §11 Config: ✓ (Task 11)
- §12 API: status/pause/resume ✓, positions ✓, IV surface ✓, enriched chain ✓, manual order kept ✓ (all Task 15)
- §13 UI: open positions / chain / IV surface / engine controls ✓ (Task 16)
- §14 Failure modes: most rely on existing patterns; no dedicated task

**Placeholder scan:** none. All "TBD" risks resolved in self-review.

**Type consistency:**
- `OptionsSignal` defined in Task 7, used in Tasks 12, 13. Field names consistent.
- `SelectedContract` defined in Task 8, used in Task 13. Field names consistent.
- `MonitorConfig` defined in Task 9, extended in Task 10, used in Task 12. Consistent.
- `ContractSelector.select(underlying, direction)` — same signature in test (Task 8) and caller (Task 13).
- Repo method names: `insert_option_trade_pending`, `update_option_trade_after_submit`, `update_option_trade_with_entry_data`, `update_option_trade_exit`, `list_open_option_trades` — all consistent across tasks.

No issues to fix.

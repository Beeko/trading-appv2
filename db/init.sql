-- Trading application schema
-- All timestamps are stored as TIMESTAMPTZ (UTC)

CREATE TABLE IF NOT EXISTS trades (
    id                  SERIAL PRIMARY KEY,
    client_order_id     VARCHAR(64) UNIQUE NOT NULL,
    broker_order_id     VARCHAR(64),
    symbol              VARCHAR(20) NOT NULL,
    side                VARCHAR(10) NOT NULL,          -- buy | sell
    order_type          VARCHAR(20) NOT NULL DEFAULT 'market',
    qty                 NUMERIC(18, 8) NOT NULL,
    filled_qty          NUMERIC(18, 8) DEFAULT 0,
    limit_price         NUMERIC(18, 4),
    stop_price          NUMERIC(18, 4),
    filled_avg_price    NUMERIC(18, 4),
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    strategy            VARCHAR(100),
    signals             TEXT[],
    signal_score        INT,
    reasoning           TEXT,
    stop_loss_price     NUMERIC(18, 4),
    take_profit_price   NUMERIC(18, 4),
    trading_mode        VARCHAR(10) NOT NULL DEFAULT 'paper',
    source              VARCHAR(20) DEFAULT 'auto',    -- auto | manual
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scanner_signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    score           INT NOT NULL,
    signals         TEXT[],
    rsi             NUMERIC(8, 4),
    volume_ratio    NUMERIC(8, 4),
    price           NUMERIC(18, 4),
    action_taken    VARCHAR(50),   -- order_placed | skipped_risk | skipped_score | skipped_max_pos | error
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id              SERIAL PRIMARY KEY,
    date            DATE UNIQUE NOT NULL,
    starting_equity NUMERIC(18, 4),
    ending_equity   NUMERIC(18, 4),
    realized_pnl    NUMERIC(18, 4) DEFAULT 0,
    num_trades      INT DEFAULT 0,
    num_wins        INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS engine_events (
    id          SERIAL PRIMARY KEY,
    event_type  VARCHAR(50) NOT NULL,
    message     TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Day-trade counter for PDT tracking (rolling 5 business days)
CREATE TABLE IF NOT EXISTS day_trades (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    filled_avg_price    NUMERIC(18, 4),                -- per-contract premium
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    trading_mode        VARCHAR(10) NOT NULL DEFAULT 'paper',
    source              VARCHAR(20) DEFAULT 'manual',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol         ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_created_at     ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status         ON trades(status);
CREATE INDEX IF NOT EXISTS idx_scanner_scanned_at    ON scanner_signals(scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_engine_events_time    ON engine_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_day_trades_date       ON day_trades(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_option_trades_underlying ON option_trades(underlying_symbol);
CREATE INDEX IF NOT EXISTS idx_option_trades_created_at ON option_trades(created_at DESC);

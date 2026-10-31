-- Project Dixon v1 — DuckDB schema
-- Markets: US | HK equities (asset_class hook for crypto/bonds phase 2)
-- Bars: persist daily / weekly / monthly (NOT derive-on-read)
-- Prices: split/dividend-adjusted OHLC from Yahoo Finance
-- Calendars: exchange session calendars (HK half-days, US early closes)
--
-- Apply: duckdb data/dixon.duckdb < schema.sql
--    or: python -m dixon.db --apply-schema

CREATE TABLE IF NOT EXISTS symbols (
  symbol       VARCHAR NOT NULL,          -- e.g. AAPL, 0700.HK, SPY
  market       VARCHAR NOT NULL,          -- 'US' | 'HK'
  exchange     VARCHAR,                   -- NYSE, NASDAQ, HKEX, ...
  name         VARCHAR,
  currency     VARCHAR,                   -- USD | HKD
  asset_class  VARCHAR NOT NULL DEFAULT 'equity',  -- equity | etf | crypto | bond (v1: equity/etf)
  is_active    BOOLEAN DEFAULT TRUE,
  yahoo_ticker VARCHAR,                   -- ticker passed to yfinance (usually = symbol)
  notes        VARCHAR,
  created_at   TIMESTAMP DEFAULT current_timestamp,
  updated_at   TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (symbol, market),
  CHECK (market IN ('US', 'HK')),
  CHECK (asset_class IN ('equity', 'etf', 'crypto', 'bond'))
);

-- Adjusted daily OHLC (Yahoo auto_adjust=True semantics)
CREATE TABLE IF NOT EXISTS daily_bars (
  market      VARCHAR NOT NULL,
  symbol      VARCHAR NOT NULL,
  trade_date  DATE NOT NULL,
  open        DOUBLE NOT NULL,
  high        DOUBLE NOT NULL,
  low         DOUBLE NOT NULL,
  close       DOUBLE NOT NULL,
  volume      BIGINT,
  source      VARCHAR DEFAULT 'yahoo',
  ingested_at TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, symbol, trade_date),
  CHECK (market IN ('US', 'HK'))
);

-- Persisted weekly bars (period_end = last session date in the week per exchange calendar)
CREATE TABLE IF NOT EXISTS weekly_bars (
  market      VARCHAR NOT NULL,
  symbol      VARCHAR NOT NULL,
  period_end  DATE NOT NULL,             -- last trading day of the week
  period_start DATE NOT NULL,            -- first trading day of the week included
  open        DOUBLE NOT NULL,
  high        DOUBLE NOT NULL,
  low         DOUBLE NOT NULL,
  close       DOUBLE NOT NULL,
  volume      BIGINT,
  bar_count   INTEGER,                   -- number of daily bars rolled up
  rebuilt_at  TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, symbol, period_end),
  CHECK (market IN ('US', 'HK'))
);

-- Persisted monthly bars (period_end = last session date in the calendar month)
CREATE TABLE IF NOT EXISTS monthly_bars (
  market      VARCHAR NOT NULL,
  symbol      VARCHAR NOT NULL,
  period_end  DATE NOT NULL,             -- last trading day of the month
  period_start DATE NOT NULL,
  open        DOUBLE NOT NULL,
  high        DOUBLE NOT NULL,
  low         DOUBLE NOT NULL,
  close       DOUBLE NOT NULL,
  volume      BIGINT,
  bar_count   INTEGER,
  rebuilt_at  TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, symbol, period_end),
  CHECK (market IN ('US', 'HK'))
);

-- Server-side indicator cache (series aligned to daily trade_date)
-- name examples: MA_20, EMA_12, RSI_14, MACD_12_26_9, MACD_SIGNAL_12_26_9,
--                MACD_HIST_12_26_9, BB_MID_20_2, BB_UPPER_20_2, BB_LOWER_20_2
CREATE TABLE IF NOT EXISTS indicator_cache (
  market      VARCHAR NOT NULL,
  symbol      VARCHAR NOT NULL,
  trade_date  DATE NOT NULL,
  name        VARCHAR NOT NULL,
  value       DOUBLE,
  computed_at TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, symbol, trade_date, name),
  CHECK (market IN ('US', 'HK'))
);

-- Ingest / job bookkeeping
CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id       VARCHAR PRIMARY KEY,
  job_type     VARCHAR NOT NULL,         -- backfill | daily | rollup | indicators
  started_at   TIMESTAMP NOT NULL,
  finished_at  TIMESTAMP,
  status       VARCHAR,                  -- running | ok | error
  detail       VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_daily_bars_date
  ON daily_bars (trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol
  ON daily_bars (market, symbol);
CREATE INDEX IF NOT EXISTS idx_weekly_bars_symbol
  ON weekly_bars (market, symbol);
CREATE INDEX IF NOT EXISTS idx_monthly_bars_symbol
  ON monthly_bars (market, symbol);
CREATE INDEX IF NOT EXISTS idx_indicator_symbol_name
  ON indicator_cache (market, symbol, name);

-- v1.2: fingerprint so on-demand MA/indicator cache can be reused until bars change.
-- `name` is the logical series (MA_20, RSI_14, …). Weekly/monthly rows in
-- indicator_cache use a timeframe prefix on `name` (see dixon/compute_indicators.py);
-- this table records which bar set that series was computed from.
CREATE TABLE IF NOT EXISTS indicator_meta (
  market       VARCHAR NOT NULL,
  symbol       VARCHAR NOT NULL,
  timeframe    VARCHAR NOT NULL,          -- daily | weekly | monthly
  name         VARCHAR NOT NULL,          -- logical name, no storage prefix
  fingerprint  VARCHAR NOT NULL,
  updated_at   TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, symbol, timeframe, name),
  CHECK (timeframe IN ('daily', 'weekly', 'monthly'))
);

-- v1.2: owner-private strategies (one symbol). rules_json is the fixed-menu rule tree.
CREATE TABLE IF NOT EXISTS strategies (
  id           VARCHAR PRIMARY KEY,
  owner_email  VARCHAR NOT NULL,
  name         VARCHAR NOT NULL,
  symbol       VARCHAR NOT NULL,
  market       VARCHAR NOT NULL,
  timeframe    VARCHAR NOT NULL DEFAULT 'daily',
  rules_json   VARCHAR NOT NULL,
  created_at   TIMESTAMP DEFAULT current_timestamp,
  updated_at   TIMESTAMP DEFAULT current_timestamp,
  CHECK (market IN ('US', 'HK')),
  CHECK (timeframe IN ('daily', 'weekly', 'monthly'))
);

CREATE INDEX IF NOT EXISTS idx_strategies_owner
  ON strategies (owner_email);

-- Overnight US Top-50 strategy board (grid s1-v1).
-- One slice is (market, grid_version, as_of). A full nightly run deletes that
-- slice and inserts the top 3 cells per symbol. Re-running the same bars is
-- bit-stable: ranks are total P&L, then fewer trades, then rule_id.
-- rank is 1..3 only. costs_flag records which cost schedule was applied
-- (us-v1.2 = CostModel defaults, no overrides).
CREATE TABLE IF NOT EXISTS strategy_board (
  market        VARCHAR NOT NULL,
  grid_version  VARCHAR NOT NULL,
  as_of         DATE NOT NULL,
  symbol        VARCHAR NOT NULL,
  rank          INTEGER NOT NULL,
  family        VARCHAR NOT NULL,
  rule_id       VARCHAR NOT NULL,
  rules_json    VARCHAR NOT NULL,
  params_json   VARCHAR NOT NULL,
  summary       VARCHAR NOT NULL,
  total_pnl     DOUBLE NOT NULL,
  trade_count   INTEGER NOT NULL,
  window_start  DATE NOT NULL,
  window_end    DATE NOT NULL,
  costs_flag    VARCHAR NOT NULL,
  computed_at   TIMESTAMP DEFAULT current_timestamp,
  PRIMARY KEY (market, grid_version, as_of, symbol, rank),
  CHECK (market IN ('US', 'HK')),
  CHECK (rank BETWEEN 1 AND 3),
  CHECK (family IN ('rsi', 'rvol', 'donchian', 'anchor'))
);

-- Optional Hive Parquet lake (written by ingest/rollup):
--   data/parquet/market={US|HK}/timeframe={daily|weekly|monthly}/year=YYYY/*.parquet

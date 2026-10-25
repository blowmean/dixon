# Project Dixon v1 — Personal HK + US Equity Market DB

Local Mac market database: Yahoo Finance adjusted OHLC, persisted daily / weekly / monthly bars, server-cached indicators, personal batch API.

**In v1:** HK + US equities (HSI + S&P 500 Day-1 approach; golden set for QA).  
**Out of v1:** live ticks, portfolio, alerts, LLM, charts UI, crypto/bonds (schema `asset_class` hook only).

PRD: see `/workspace/dixon-prd/Dixon.md` (or your copy). Evolved from `market-data-duckdb/` sketch.

---

## Requirements (Mac)

- Python 3.11+ (3.12/3.13 fine)
- ~few hundred MB disk for golden set × 5y; more for full HSI+S&P500
- Network for Yahoo Finance downloads

## Setup

```bash
cd project-dixon
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .          # installs `dixon` package
```

Optional env (defaults shown):

```bash
export DIXON_DB="$(pwd)/data/dixon.duckdb"
export DIXON_PARQUET_ROOT="$(pwd)/data/parquet"
export DIXON_YAHOO_SLEEP=0.75          # seconds between tickers
export DIXON_HISTORY_YEARS=5
export DIXON_RESTATE_DAYS=14         # daily job overwrite window
export DIXON_API_HOST=127.0.0.1
export DIXON_API_PORT=8765
export DIXON_API_KEY=                  # empty = no auth; set for X-API-Key header
```

## Apply schema

```bash
python -m dixon.db --apply-schema
# or: duckdb data/dixon.duckdb < schema.sql
```

## One-shot backfill (golden set, ~5 years)

```bash
python -m dixon.ingest --golden --years 5 --parquet
python -m dixon.rollup --all --parquet
python -m dixon.compute_indicators --all
```

Full Day-1 file (golden + placeholders):

```bash
python -m dixon.ingest --day1 --years 5
```

Custom symbols:

```bash
python -m dixon.ingest --symbols 0700.HK,AAPL,MSFT --years 5
```

## Daily job (rate-limited catch-up + restates)

One command (preferred):

```bash
./scripts/daily_job.sh
# equivalent:
python -m dixon.daily_job
python -m dixon.daily_job --restate-days 14 --golden
python -m dixon.daily_job --symbols AAPL,0700.HK --restate-days 7
```

### What it does

1. **Ingest** active symbols (or `--symbols` / `--golden`) for the last `DIXON_RESTATE_DAYS` **calendar** days (default **14**).
2. **Overwrite** that window with `INSERT OR REPLACE` so Yahoo soft restates (adjusted OHLC corrections) land in `daily_bars`.
3. **Diff** each symbol vs DB: only symbols with **new or changed** bars are *affected*.
4. **Rollup** weekly/monthly **only for affected** symbols (not the whole universe).
5. **Recompute** indicator cache **only for affected** symbols.
6. Per-symbol Yahoo failures are **soft** — logged; other symbols continue.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All symbols succeeded (or nothing to do) |
| 1 | Fatal (DB/config/crash) |
| 2 | Partial — some tickers failed; others updated |

### Env / flags

| Name | Default | Role |
| --- | --- | --- |
| `DIXON_RESTATE_DAYS` / `--restate-days` | 14 | Calendar-day lookback to re-download & overwrite |
| `DIXON_YAHOO_SLEEP` | 0.75 | Seconds between Yahoo ticker calls |
| `--parquet` | off | Also refresh Hive Parquet partitions |
| `--force-rebuild` | off | Rebuild W/M+indicators for every OK symbol even if OHLC unchanged |

Ingest-only (no rollup/indicators):

```bash
python -m dixon.ingest --daily --restate-days 14
```

### Scheduling on Mac (launchd / cron)

Run after both HK and US cash sessions are settled (e.g. **06:30 HKT** next calendar day covers US close).

**cron** (user crontab):

```cron
# m h dom mon dow
30 6 * * 1-5  cd /path/to/project-dixon && ./scripts/daily_job.sh >>logs/daily_job.log 2>&1
```

**launchd** sketch (`~/Library/LaunchAgents/com.paul.dixon.daily.plist`):

```xml
<!-- StartInterval or StartCalendarInterval weekdays 06:30 local -->
<!-- ProgramArguments: /path/to/project-dixon/scripts/daily_job.sh -->
<!-- WorkingDirectory: /path/to/project-dixon -->
<!-- StandardOutPath / StandardErrorPath → logs -->
```

Keep Mac awake or allow power-nap wake; if a day is skipped, the next run’s 14-day restate window catches up missed sessions and soft restates.

Yahoo is best-effort SoT: rate-limit hard, expect gaps/restates; the daily job is the recovery path.

## Personal batch API

```bash
python -m dixon.api
# → http://127.0.0.1:8765/docs
```

| Endpoint | Notes |
| --- | --- |
| `GET /health` | DB path + row counts; includes `batch_max_symbols` |
| `GET /symbols` | Active tickers for pickers |
| `GET /bars?symbol=AAPL&timeframe=daily&from=2024-01-01&to=2024-12-31` | Single symbol; `timeframe=daily\|weekly\|monthly` |
| `GET /bars/batch?symbols=AAPL,MSFT,0700.HK&timeframe=daily&from=&to=` | Multi-ticker (max **50**); market inferred per ticker (`.HK`→HK) unless `market=` set for all |
| `GET /indicators?symbol=AAPL&name=RSI_14&from=2024-01-01` | Cached series |

Optional: `Header X-API-Key: $DIXON_API_KEY` when key is set.

Empty single-symbol `bars` with `count: 0` means no ingested data (not an error).

### Batch response (JSON, default)

```json
{
  "timeframe": "daily",
  "from": null,
  "to": null,
  "count": 3,
  "bar_count": 3754,
  "max_symbols": 50,
  "series": {
    "AAPL": { "market": "US", "count": 1260, "bars": [ {"date":"...","open":...} ] },
    "0700.HK": { "market": "HK", "count": 1234, "bars": [ ... ] }
  },
  "missing": ["NOSUCH"]
}
```

`missing` = requested symbols that returned **zero** bars.

### Batch CSV download

```bash
# query param
curl -o bars.csv 'http://127.0.0.1:8765/bars/batch?symbols=AAPL,MSFT,0700.HK&timeframe=daily&format=csv'
# or Accept header
curl -H 'Accept: text/csv' -o bars.csv \
  'http://127.0.0.1:8765/bars/batch?symbols=AAPL,MSFT,SPY&timeframe=weekly&from=2024-01-01'
```

CSV columns: `symbol,market,timeframe,date,open,high,low,close,volume`.


## Charts UI

Candlestick charts (TradingView **lightweight-charts**, vendored) with server-cached indicator overlays.

```bash
python -m dixon.api
# open http://127.0.0.1:8765/   (redirects to /ui/)
```

- Symbol picker: DB active symbols (falls back to golden set)
- Timeframe: daily / weekly / monthly (adjusted OHLC from `/bars`)
- Price pane: MA / EMA / Bollinger from `/indicators` (`MA_20`, `MA_50`, `MA_200`, `EMA_12`, `EMA_26`, `BB_*_20_2`)
- Lower panes: `RSI_14`, `MACD_12_26_9` + signal + hist (see `indicators/FORMULAS.md`)
- Weekly/monthly: overlays align daily cache points to each bar’s `period_end` (no browser recompute)
- Optional API key: paste into the UI field (sent as `X-API-Key`); stored in `localStorage`

Static files: `dixon/static/` (`index.html`, `app.js`, `styles.css`, `vendor/lightweight-charts…`).

## Layout

```
project-dixon/
  README.md
  requirements.txt
  pyproject.toml
  schema.sql
  dixon/                 # Python package
    config.py
    db.py
    calendars.py         # NYSE + HKEX via exchange_calendars
    yahoo.py
    ingest.py
    rollup.py
    compute_indicators.py
    daily_job.py         # orchestrated daily catch-up
    api.py
    static/              # charts UI (lightweight-charts)
  indicators/FORMULAS.md # QA formula sheet
  universe/
    golden.txt           # 0700.HK 0005.HK AAPL MSFT SPY
    day1.txt             # golden + HSI/S&P500 placeholders
  scripts/
    backfill.sh
    daily_job.sh
  data/                  # created at runtime (gitignored)
    dixon.duckdb
    parquet/market=/timeframe=/year=/
```

## Calendars

- US → `exchange_calendars` **XNYS** (early closes respected as sessions)
- HK → **XHKG** (half-days respected as sessions)
- Weekly/monthly rollups group **available daily bars** by ISO week / calendar month (see `indicators/FORMULAS.md`)
- If `exchange-calendars` fails to install: Mon–Fri fallback with a warning (holidays wrong)

## Blockers / caveats

- Yahoo rate limits — keep `DIXON_YAHOO_SLEEP` ≥ 0.5–1.0s; full S&P500 backfill takes a long time
- HK adjust factors on Yahoo can be weaker than US
- Silent Yahoo history rewrites → re-backfill lookback + rollup + indicators
- Crypto/bonds: `asset_class` column only; no v1 ingest path

## Smoke test

```bash
python -m dixon.db --apply-schema
python -m dixon.ingest --symbols AAPL --years 5
python -m dixon.rollup --symbols AAPL
python -m dixon.compute_indicators --symbols AAPL
python -m dixon.api &
curl -s 'http://127.0.0.1:8765/health'
curl -s 'http://127.0.0.1:8765/bars?symbol=AAPL&timeframe=daily&from=2025-01-01' | head
```

## Host

Served from **darkstar** (/home/paul/project-dixon). See [MIGRATE.md](MIGRATE.md) for systemd start/stop.

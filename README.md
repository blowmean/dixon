# Project Dixon v1 — Personal HK + US Equity Market DB

Local Mac market database: Yahoo Finance adjusted OHLC, persisted daily / weekly / monthly bars, server-cached indicators, personal batch API.

**In v1 / v1.2:** HK + US equities, persisted D/W/M bars, charts with volume, cached indicators, Top-50 turnover screener, owner-only strategy backtests.  
**Out:** live ticks, broker execution, alerts, shared strategies, fundamentals, crypto/bonds (schema `asset_class` hook only).

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
export DIXON_DEFAULT_OWNER=paul.kf.leung@gmail.com
```

`DIXON_DEFAULT_OWNER` is the strategy owner for localhost and `X-API-Key` callers. A Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`) attributes strategies to its `email` claim instead.

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
| `GET /indicators?symbol=AAPL&name=RSI_14&from=2024-01-01` | One cached series. `timeframe=daily\|weekly\|monthly` (default daily). Missing series are computed and cached |
| `GET /indicators?symbol=AAPL&names=MA_5,MA_20&timeframe=weekly` | Several series in one response (`series` is a map) |
| `GET /screener/top50?market=HK\|US&date=YYYY-MM-DD` | That market's Top-50 by turnover. `date` defaults to the latest session |
| `GET /strategies` | Strategies owned by the caller |
| `POST /strategies` | Create one (`name`, `symbol`, `timeframe`, `rules`) |
| `GET /strategies/{id}` | Owner-only |
| `PUT /strategies/{id}` | Replace name, symbol, timeframe, rules |
| `DELETE /strategies/{id}` | Owner-only |
| `POST /strategies/{id}/backtest` | Body: `start`, `end`, optional `costs` |

Optional: `Header X-API-Key: $DIXON_API_KEY` when key is set. Browser traffic may send `Cf-Access-Jwt-Assertion` instead.

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
- Price pane: candlesticks with the bottom ~25% reserved for the volume histogram (`priceScale("right")` bottom 0.25, volume scale top 0.75)
- SMA menu: 5, 9, 20, 30, 50, 100, 250, plus existing MA 200 / EMA 12 / EMA 26 / Bollinger
- Lower panes: `RSI_14`, `MACD_12_26_9` + signal + hist (see `indicators/FORMULAS.md`)
- Weekly/monthly: overlays are computed on that timeframe's bars (cached on first use)
- Top-50 panel and a private strategy editor (save, then run a dated backtest: equity curve, trades, win rate, max drawdown)
- No API-key field. Same-origin Access or localhost; scripts may still send `X-API-Key`

Static files: `dixon/static/` (`index.html`, `app.js`, `styles.css`, `vendor/lightweight-charts…`).

## v1.2 screener, strategies, backtest

### Top-50 (`GET /screener/top50?market=HK|US&date=YYYY-MM-DD`)

Separate HK and US pools, ranked by turnover = close × volume in **local currency** (no FX merge). `date` defaults to the latest session in that market. Fewer than 50 names returns the whole pool.

A row's `spike` is true only when that symbol is **inside** the pool and all three hold:

- close > prior 5-session high (max `high` of the previous 5 bars)
- day gain (close / previous close − 1) ≥ 5%
- SMA(30) rising: SMA30_t > SMA30_{t−1}

### Strategy rules

One symbol per strategy. `timeframe` is `daily`, `weekly`, or `monthly`. Each of `long`, `flat`, and `short` is `{logic: AND|OR, conditions: [...]}`.

| `type` | Fields | Meaning |
| --- | --- | --- |
| `price_gt_ma` / `price_lt_ma` | `window` | close vs SMA(window) |
| `ma_slope_gt_0` / `ma_slope_lt_0` | `window` | SMA_t vs SMA_{t−1} |
| `ma_stack` | `windows` (e.g. `[5,20,50]`), `direction` `up` or `down` | 5>20>50 or 5<20<50 |
| `top50_spike` | — | this bar's session (weekly/monthly: `period_end`) is a daily Top-50 spike |
| `rel_volume` | `k` (default **1.5**), `window` (default **20**) | signal-bar volume ≥ k × SMA(volume, N). The average **includes** the signal bar |
| `rsi_lte` / `rsi_gte` | `period` (default **2**), `threshold` (default **5** / **70**) | Wilder RSI(period) ≤ or ≥ threshold. Same formula as `RSI_14`, period not fixed |
| `donchian_high` | `window` (default **20**) | close > max `high` of the previous N bars (the signal bar is excluded) |
| `donchian_low` | `window` (default **10**) | close < min `low` of the previous M bars (the signal bar is excluded) |
| `roc_gte` / `roc_lte` | `period` (default **7**), `threshold` (default **+1** / **−1**) | ROC(period) ≥ or ≤ threshold. ROC is percent points: `(close / close N bars ago − 1) × 100`. **1 means +1%**, not 100% and not the fraction 0.01 |

Price-SMA windows must be the fixed menu: 5, 9, 20, 30, 50, 100, 250. Relative-volume, Donchian, and ROC lookbacks are any integer from 1 to 500; RSI period is 1 to 250 and its threshold is 0 to 100. ROC threshold is −100 to 10000 percent points. Omitted knobs are stored as the defaults above. Lookbacks count **bars of the strategy timeframe** (a daily strategy's 20 is 20 sessions, not calendar days).

Optional top-level `trail_pct` is a trailing stop in **percent points** (5 means 5%, not 0.05). Omit it, or send null, and the stop is off — older rules are unchanged. While a position is open, the extreme is the peak **high** (long) or trough **low** (short) since the fill, including the current bar. A close at or through `extreme × (1 ∓ trail_pct/100)` forces flat even if that side's entry predicate is still true. The fill is still the **next stored bar's open**, with `exit_reason` `"trail"` (the exit price is that open, not the stop level). An opposite-side signal on the same close reverses (`exit_reason` `"signal"`) instead of flattening. A trail breach on the last bar does not invent a flatten. The ROC(7) public pattern fills `trail_pct` **5**.

An empty side never fires. If long and short both fire, the target is flat; otherwise long, then short, then the flat rule, otherwise hold. Put signal exits on `flat`: a flat rule does not override a long or short rule that is still true. The trail is the exception: it overrides a still-true entry on the held side. The fill is the **next stored bar's open**.

In `/ui/`, open **Strategies**. Each condition row has the extra knobs when the type needs them (k and N, RSI period and threshold, Donchian N or M, ROC period and ±% threshold). **Trail % from peak** is the optional stop (blank = off). The **Public patterns** buttons fill a volume-confirmed SMA(20) breakout, a Connors-style RSI(2) ≤ 5 entry that exits on close > SMA(5) or RSI(2) ≥ 70, a Donchian 20/10 channel, and ROC(7) ±1% with a 5% trail. Edit the numbers, Save, then run a dated backtest. ATR stops are not part of this slice. ROC composes with the other predicates: put `rel_volume`, `rsi_lte` / `rsi_gte`, Donchian, or an MA rule on the same side with `logic: AND`.

```bash
curl -s -X POST localhost:8765/strategies \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "AAPL SMA20",
    "symbol": "AAPL",
    "timeframe": "daily",
    "rules": {
      "long": {"logic": "AND", "conditions": [
        {"type": "price_gt_ma", "window": 20},
        {"type": "ma_slope_gt_0", "window": 20},
        {"type": "top50_spike"}
      ]},
      "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 20}]},
      "short": {"logic": "AND", "conditions": []}
    }
  }'
```

List, get, update, and delete only return rows for the owning email. Another Access email gets 404.

### Backtest (`POST /strategies/{id}/backtest`)

```json
{ "start": "2024-01-01", "end": "2024-12-31", "costs": {} }
```

Omit `costs` (or leave a key out) for the PRD defaults:

| | HK | US |
| --- | --- | --- |
| Capital | HKD 500,000 | USD 100,000 |
| Commission | 25 bps of notional / side | $0.005 / share, minimum $1 |
| Bid-ask | spread width **2 ticks** on the HKEX price-band table; **each fill pays half** (1 tick / side) | **5 bps of price each side** |

Sizing is 20% of marked equity per entry (`equity_fraction`). The book allows concurrent names while gross entry notional fits inside equity (about five 20% tickets) and refuses a sixth. A strategy has one symbol, so one run holds at most one position. Shorts are synthetic (no borrow). Dividends are ignored. An open position at `end` is marked to that bar's close (`open_position`); it is not force-flattened.

`metrics.win_rate` and `metrics.max_drawdown` are display-only against targets 50% and 10%. They do not fail the run.

```bash
python scripts/golden_backtest.py
# or: python -m pytest tests/test_golden_backtest.py
```

The golden case is AAPL, 2024-01-02 through 2024-01-15, long while close > SMA(5) and flat while close < SMA(5), default US costs: **1 trade, P&L −6,700, ending equity 93,300**.

Implementation choices worth knowing on the live host:

- Fills use the next **stored** bar (the exchange calendar is already baked into `daily_bars` / rollups).
- HK “2 ticks” is the spread width. Each side pays half of that (1 tick at the default), using the HKEX band for the fill price.
- Shares are fractional. HK board lots are not rounded.
- One strategy, one symbol, so a single backtest holds one position. The book still sizes at 20% and will refuse a sixth concurrent name or a ticket that would push gross entry notional above equity.
- The JWT email is read but not signature-checked here. Cloudflare Access already authenticated the browser; localhost and API-key calls cannot present that header from the public hostname (see `_check_key`).

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
curl -s 'http://127.0.0.1:8765/screener/top50?market=US'
python scripts/golden_backtest.py
```

## Host

Served from **darkstar** (/home/paul/project-dixon). See [MIGRATE.md](MIGRATE.md) for systemd start/stop.

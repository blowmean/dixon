# Project Dixon — Market DB PRD v1.1

Approved in Project Dixon group chat · 2026-10-23 (HKT)  
**v1.1 amendment** · 2026-10-24 (HKT): public HTTPS exposure via Cloudflare on `robonet.cc`  
**Go-live** · 2026-10-24 (HKT): `https://dixon.robonet.cc/ui/` via Tunnel → origin `:8765`; Access + `DIXON_API_KEY`  
**v1.2 amendment** · 2026-10-25 (HKT): volume histogram, Top-50 screener, fixed-menu MAs, owner-only strategy backtest. Endpoint notes are in [README](README.md) and below. The sealed chat PRD is the product source of truth; this file records what the repo implements.

## Status

**Public UI live:** https://dixon.robonet.cc/ui/  
Tunnel: `dixon-mac11` → mac11 `127.0.0.1:8765`  
Auth: Cloudflare Access for browser UI (no key paste); optional `DIXON_API_KEY` for scripts  
Local UI still: http://127.0.0.1:8765/ui/

## Goal

Personal market database that stores OHLC (daily, weekly, monthly), exposes a batch download API, and provides a web UI with candlestick charts plus technical indicators — available on **local Mac** and over **HTTPS on the public internet** under the existing `robonet.cc` zone.

## v1 decisions (locked)

| Topic | Decision |
| --- | --- |
| Universe | **Phase A (locked):** HSI + HSTECH + S&P 500 + Nasdaq-100 + ETFs in-scope; golden set retained. Full exchange lists (~14.7k) on disk for later B/C — no uncapped ingest yet |
| Feed | Yahoo Finance only (no paid vendor); personal research use |
| Prices | Split/dividend-adjusted OHLC only |
| Bars | Persist daily, weekly, monthly (not derive-on-read) |
| Calendars | Exchange trading calendars (HK half-days, US early closes) |
| History | 5 years |
| Refresh | One-shot backfill + daily job; rate-limit Yahoo hard |
| API clients | Paul + authenticated public HTTPS clients |
| Hosting | Primary compute on local Mac (`/Users/paul/project-dixon`); public edge via Cloudflare |
| Charts | Adjusted OHLC candlesticks |
| Indicators | Server precompute/cache: MA, EMA, RSI, MACD, Bollinger; best-effort vs Yahoo math (not TradingView-certified) |
| Crypto / bonds | Phase 2 (schema-ready; not Day-1 ingest) |

## Public exposure (v1.1)

**UX lock (2026-10-24):** Asking users to paste `DIXON_API_KEY` in the public UI is **not acceptable**.


| Topic | Decision |
| --- | --- |
| Domain zone | `robonet.cc` (existing Cloudflare account; other agent keeps apex/`www`) |
| Hostname | **`dixon.robonet.cc`** (subdomain; path-based `/dixon` only if Paul overrides) |
| Edge | Cloudflare Tunnel (`cloudflared`) from mac11 → `http://127.0.0.1:8765` |
| Auth (must) | **Browser:** Cloudflare Access only (no API-key field in `/ui/`). **Scripts:** optional `DIXON_API_KEY` / `X-API-Key`. API accepts Access JWT (`Cf-Access-Jwt-Assertion`) for same-origin UI traffic |
| Bind | Keep Dixon on `127.0.0.1` locally; do **not** expose raw `0.0.0.0` to the internet (tunnel only) |
| Availability | Public UI only while mac11 is online and Tunnel is running |
| Secrets | Cloudflare tokens via masked secret card / Vault / Cloudflare OAuth plugin — **never in group chat** |


**Universe expand (2026-10-24):** Paul chose **A**. ETFs **in** (default; aligns with golden SPY). Eng: build Phase A list + rate-limited 5y backfill; searchable UI before list grows.

## In scope

- Symbol registry with `asset_class` (equity first); expand later
- Ingest: Yahoo backfill + daily EOD job for HK and US sessions
- Storage: persisted daily / weekly / monthly bars (tables or Parquet partitions)
- Rebuild weekly/monthly when daily updates or restates land
- Batch API: download OHLC by symbol(s) and date range (local + authenticated public)
- Web UI: candlestick charts on adjusted OHLC
- Server-cached indicators: MA, EMA, RSI, MACD, Bollinger
- Documented indicator formula sheet for QA
- Cloudflare Tunnel + DNS for `dixon.robonet.cc`
- Auth gate (Cloudflare Access and/or API key) before public go-live

## Out of scope / phase 2

- Live ticks / intraday
- Portfolio, positions, P&L
- Alerts / notifications
- Local-LLM chart or signal explanations
- TradingView-certified indicator parity
- Crypto (24/7 UTC bars) and bonds — separate ingest path later; prefer liquid bond ETFs (e.g. TLT/IEF) over individual CUSIPs on Yahoo
- Uncapped Day-1 universe — schema may allow more symbols; ingest starts capped
- Always-on cloud host (Mac is origin unless Paul later moves compute)

## Architecture notes

- **Suggested store:** DuckDB + Parquet (reuse eng sketch for ~10y equity daily; size for 5y HK+US)
- **Bar rebuild:** Weekly/monthly rebuilt from daily on exchange calendars after daily land or restates
- **Yahoo posture:** Best-effort SoT — gaps, delays, silent history rewrites possible; rate-limit the daily job
- **Local API:** `127.0.0.1:8765` on mac11; UI at `/ui/`
- **Public path:** Cloudflare Tunnel → local API; DNS CNAME for `dixon.robonet.cc`
- **Coexistence:** Other agent service retains apex/`www` on `robonet.cc`; Dixon is subdomain-only

## Acceptance (QA)

Correctness is signed off on a small golden-symbol set. Broader universe is smoke/coverage only until expand.

Proposed golden set: `0700.HK`, `0005.HK`, `AAPL`, `MSFT`, `SPY`

Checks:

- Adjusted D/W/M vs Yahoo within 1 tick
- W/M consistent with daily under exchange calendars (including after restates)
- MA/EMA/RSI/MACD/Bollinger match the formula sheet vs Yahoo-derived series
- Batch API: download by symbol/range; clear errors for bad range/symbol
- Charts: candles render adjusted OHLC; indicators overlay from server cache
- **Public (v1.1):** Access login → charts load with **no** API-key field; unauthenticated API blocked; scripts may use `X-API-Key`; Tunnel failure fails closed

## Known risks

- Yahoo rate limits and overnight job length constrain universe size
- HK adjust factors on Yahoo can be weaker than US
- Silent Yahoo history rewrites — need restate/rebuild path
- HK vs US session calendars — job must respect both closes
- Local Mac downtime — daily job may skip; public site down when Mac/Tunnel off
- Public exposure of Yahoo-derived data — auth required; watch Cloudflare/Yahoo ToS and redistributability

## v1.2 implementation notes

Shipped in this repo (research / backtest only):

| Piece | Where |
| --- | --- |
| Volume histogram under candles, bottom ~25% of the price pane | `dixon/static/app.js` (`priceScale("right")` bottom 0.25, volume scale top 0.75) |
| Top-50 HK and US pools + spike flags | `GET /screener/top50?market=HK\|US&date=YYYY-MM-DD` |
| SMA menu 5, 9, 20, 30, 50, 100, 250 on D/W/M, on-demand DuckDB cache | `GET /indicators` (`name` or `names`, `timeframe`) |
| Owner-private strategy CRUD, one symbol | `GET/POST /strategies`, `GET/PUT/DELETE /strategies/{id}` |
| Next-open backtest, 20% equity, synthetic shorts, dated P&L | `POST /strategies/{id}/backtest` |
| Golden P&L lock | `python scripts/golden_backtest.py` |

Spike rule (inside that market's Top-50 only): close > prior 5-session high AND day gain ≥ 5% AND SMA30 rising. Turnover is close × volume in local currency.

Owner: Access JWT `email` claim when `Cf-Access-Jwt-Assertion` is present; otherwise `DIXON_DEFAULT_OWNER` (default `paul.kf.leung@gmail.com`) for localhost and `X-API-Key`.

Cost defaults when the backtest body leaves them blank: HK commission 25 bps and a 2-tick HKEX spread (half charged each side); US 5 bps each side and $0.005/share with a $1 minimum. Capital HKD 500,000 / USD 100,000. Win rate target > 50% and max drawdown target 10% are returned on `metrics` and are not pass/fail gates.

Out of this ship: broker routing, alerts, shared strategies, fundamentals, sector, 60%-off-low, 5/10 MA stop, a second vendor.

### S1 signal predicates (one symbol)

Strategy conditions, same CRUD and next-open backtest. Knobs live on each condition and in the Strategies panel (including three public-pattern fillers).

| Predicate | Default | Rule |
| --- | --- | --- |
| `rel_volume` | k=1.5, N=20 | volume ≥ k × SMA(volume, N), average includes the signal bar |
| `rsi_lte` / `rsi_gte` | period 2, threshold 5 / 70 | Wilder RSI(period) compared with the threshold |
| `donchian_high` / `donchian_low` | N=20 / M=10 | close > prior N-bar high, or close < prior M-bar low |

Not in this slice: Minervini RS rank, cross-sectional momentum, a shared multi-name book, ATR stops.

## Next steps

1. Paul delivers Cloudflare credentials via **1:1 masked secret** or Cloudflare OAuth plugin (not group chat)
2. Eng: Tunnel + DNS for `dixon.robonet.cc` + auth gate
3. QA: public HTTPS + auth acceptance
4. Expand Day-1 universe / phase-2 assets later as needed

### Owners

| Role | Owner |
| --- | --- |
| Schema / ingest / API / Tunnel | Software Engineer |
| Delivery | Software Manager |
| Golden set / acceptance (incl. public auth) | QA Manager |
| Universe / Yahoo caveats | Financial Expert |
| ML / LLM path | MLX Agent (out of v1) |
| This PRD | Project Manager |

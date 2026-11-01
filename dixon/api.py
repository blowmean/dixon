"""Personal batch API + charts UI for Project Dixon (local-only).

Bind 127.0.0.1 by default. Optional API key via DIXON_API_KEY env.

  uvicorn dixon.api:app --host 127.0.0.1 --port 8765
  python -m dixon.api

Charts UI: http://127.0.0.1:8765/  (or /ui/)
Batch OHLC: GET /bars/batch?symbols=AAPL,MSFT,0700.HK&timeframe=daily
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from pydantic import BaseModel, Field

from dixon.auth import owner_email
from dixon.backtest import run_backtest
from dixon.compute_indicators import (
    ensure_indicators,
    list_indicator_names,
    read_indicator,
)
from dixon.config import API_HOST, API_KEY, API_PORT, DB_PATH, GOLDEN_FILE
from dixon.db import connect, session
from dixon.screener import build_top50
from dixon.strategy_board import GRID_VERSION, read_board
from dixon.strategies import (
    create_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    update_strategy,
)
from dixon.ui_static import UiStaticFiles
from dixon.yahoo import infer_market

STATIC_DIR = Path(__file__).resolve().parent / "static"
BATCH_MAX_SYMBOLS = 50
Timeframe = Literal["daily", "weekly", "monthly"]

app = FastAPI(title="Project Dixon API", version="0.1.0", docs_url="/docs")


def _check_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    cf_access_jwt_assertion: Optional[str] = Header(
        default=None, alias="Cf-Access-Jwt-Assertion"
    ),
) -> None:
    """Auth: DIXON_API_KEY, or Cloudflare Access JWT, or localhost UI.

    Public browser traffic is already gated by Cloudflare Access; requiring a
    second pasted key in the charts UI is not acceptable. Scripts can still
    send X-API-Key (or an Access service-token JWT).
    """
    if not API_KEY:
        return
    if x_api_key == API_KEY:
        return
    # Browser session after Access login (header forwarded by Cloudflare).
    if cf_access_jwt_assertion:
        return
    # Local Mac UI (Host is 127.0.0.1/localhost). Do NOT trust client IP alone:
    # cloudflared also connects from 127.0.0.1 with Host=dixon.robonet.cc — that path
    # must present Cf-Access-Jwt-Assertion (or X-API-Key).
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def require_owner(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    cf_access_jwt_assertion: Optional[str] = Header(
        default=None, alias="Cf-Access-Jwt-Assertion"
    ),
) -> str:
    """Authenticated owner email. Access JWT wins; otherwise the default owner."""
    _check_key(request, x_api_key, cf_access_jwt_assertion)
    try:
        return owner_email(cf_access_jwt_assertion)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: Literal["daily", "weekly", "monthly"] = "daily"
    rules: dict[str, Any]


class BacktestBody(BaseModel):
    start: date
    end: date
    costs: Optional[dict[str, Any]] = None


def _iso_dates(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    return out


def _fetch_bars(
    con,
    symbol: str,
    market: str,
    timeframe: Timeframe,
    from_: date | None,
    to: date | None,
) -> list[dict[str, Any]]:
    table = {
        "daily": "daily_bars",
        "weekly": "weekly_bars",
        "monthly": "monthly_bars",
    }[timeframe]
    date_col = "trade_date" if timeframe == "daily" else "period_end"
    clauses = ["market = ?", "symbol = ?"]
    params: list[Any] = [market, symbol]
    if from_ is not None:
        clauses.append(f"{date_col} >= ?")
        params.append(from_)
    if to is not None:
        clauses.append(f"{date_col} <= ?")
        params.append(to)
    where = " AND ".join(clauses)
    if timeframe == "daily":
        select = "trade_date AS date, open, high, low, close, volume"
    else:
        select = (
            "period_end AS date, period_start, open, high, low, close, volume, bar_count"
        )
    rows = con.execute(
        f"SELECT {select} FROM {table} WHERE {where} ORDER BY {date_col}",
        params,
    ).fetchdf()
    return [_iso_dates(r) for r in rows.to_dict(orient="records")]


def _parse_symbols(symbols: str) -> list[str]:
    """Comma-separated tickers → unique uppercase list (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for part in symbols.split(","):
        s = part.strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _resolve_market(symbol: str, market: str | None) -> str:
    market_u = (market or infer_market(symbol)).upper()
    if market_u not in {"US", "HK"}:
        raise HTTPException(400, "market must be US or HK")
    return market_u


def _wants_csv(request: Request, format_: str | None) -> bool:
    if format_ and format_.lower() == "csv":
        return True
    accept = (request.headers.get("accept") or "").lower()
    # Prefer explicit csv over */* from curl default
    if "text/csv" in accept and "application/json" not in accept.split(",")[0]:
        return True
    if accept.startswith("text/csv"):
        return True
    return False


def _batch_csv(
    series: dict[str, list[dict[str, Any]]],
    markets: dict[str, str],
    timeframe: str,
) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["symbol", "market", "timeframe", "date", "open", "high", "low", "close", "volume"]
    )
    for symbol, bars in series.items():
        mkt = markets[symbol]
        for b in bars:
            writer.writerow(
                [
                    symbol,
                    mkt,
                    timeframe,
                    b.get("date"),
                    b.get("open"),
                    b.get("high"),
                    b.get("low"),
                    b.get("close"),
                    b.get("volume"),
                ]
            )
    data = buf.getvalue()
    filename = f"dixon_bars_{timeframe}.csv"
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health(_: None = Depends(_check_key)):
    try:
        con = connect(apply_schema=True)
        n = con.execute("SELECT count(*) FROM daily_bars").fetchone()[0]
        syms = con.execute("SELECT count(*) FROM symbols").fetchone()[0]
        con.close()
        return {
            "status": "ok",
            "db": str(DB_PATH),
            "symbols": syms,
            "daily_bars": n,
            "ui": "/ui/",
            "batch_max_symbols": BATCH_MAX_SYMBOLS,
        }
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(e)},
        )


@app.get("/symbols")
def list_symbols(_: None = Depends(_check_key)):
    """Active symbols for the chart picker (DB first, else golden set file)."""
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT symbol, market, asset_class, currency
            FROM symbols
            WHERE is_active
            ORDER BY market, symbol
            """
        ).fetchall()
    finally:
        con.close()

    if rows:
        return {
            "count": len(rows),
            "symbols": [
                {
                    "symbol": r[0],
                    "market": r[1],
                    "asset_class": r[2],
                    "currency": r[3],
                }
                for r in rows
            ],
        }

    golden: list[str] = []
    if GOLDEN_FILE.exists():
        for line in GOLDEN_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            golden.append(line.split("#", 1)[0].strip().upper())
    return {
        "count": len(golden),
        "symbols": [
            {
                "symbol": s,
                "market": infer_market(s),
                "asset_class": None,
                "currency": None,
            }
            for s in golden
        ],
    }


@app.get("/bars")
def get_bars(
    symbol: str = Query(..., description="Ticker e.g. AAPL or 0700.HK"),
    timeframe: Timeframe = Query("daily"),
    market: Optional[str] = Query(None, description="US|HK; inferred if omitted"),
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    _: None = Depends(_check_key),
):
    symbol = symbol.strip().upper()
    market_u = _resolve_market(symbol, market)
    con = connect()
    try:
        records = _fetch_bars(con, symbol, market_u, timeframe, from_, to)
    finally:
        con.close()
    return {
        "market": market_u,
        "symbol": symbol,
        "timeframe": timeframe,
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
        "count": len(records),
        "bars": records,
    }


@app.get("/bars/batch")
def get_bars_batch(
    request: Request,
    symbols: str = Query(
        ...,
        description=f"Comma-separated tickers (max {BATCH_MAX_SYMBOLS}), e.g. AAPL,MSFT,0700.HK",
    ),
    timeframe: Timeframe = Query("daily"),
    market: Optional[str] = Query(
        None,
        description="Optional single market applied to ALL symbols; else infer per ticker (.HK→HK)",
    ),
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    format_: Optional[str] = Query(
        None,
        alias="format",
        description="json (default) or csv; also Accept: text/csv",
    ),
    _: None = Depends(_check_key),
):
    """Multi-ticker OHLC download.

    JSON shape:
      {
        "timeframe": "daily",
        "from": "...", "to": "...",
        "count": <symbols requested>,
        "bar_count": <total bars across series>,
        "series": { "AAPL": { "market": "US", "count": N, "bars": [...] }, ... },
        "missing": ["NOSUCH"]   # requested symbols with zero bars
      }

    CSV (`?format=csv` or `Accept: text/csv`):
      symbol,market,timeframe,date,open,high,low,close,volume
    """
    tickers = _parse_symbols(symbols)
    if not tickers:
        raise HTTPException(400, "symbols must list at least one ticker")
    if len(tickers) > BATCH_MAX_SYMBOLS:
        raise HTTPException(
            400,
            f"Too many symbols ({len(tickers)}); max is {BATCH_MAX_SYMBOLS} per request",
        )

    if market is not None:
        market_global = market.upper()
        if market_global not in {"US", "HK"}:
            raise HTTPException(400, "market must be US or HK")
    else:
        market_global = None

    series: dict[str, dict[str, Any]] = {}
    markets: dict[str, str] = {}
    flat_for_csv: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    total_bars = 0

    con = connect()
    try:
        for sym in tickers:
            mkt = market_global or infer_market(sym)
            bars = _fetch_bars(con, sym, mkt, timeframe, from_, to)
            markets[sym] = mkt
            flat_for_csv[sym] = bars
            series[sym] = {"market": mkt, "count": len(bars), "bars": bars}
            total_bars += len(bars)
            if not bars:
                missing.append(sym)
    finally:
        con.close()

    if _wants_csv(request, format_):
        return _batch_csv(flat_for_csv, markets, timeframe)

    return {
        "timeframe": timeframe,
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
        "count": len(tickers),
        "bar_count": total_bars,
        "max_symbols": BATCH_MAX_SYMBOLS,
        "series": series,
        "missing": missing,
    }


def _indicator_names(name: str | None, names: str | None) -> list[str]:
    if names:
        parsed = []
        seen: set[str] = set()
        for part in names.split(","):
            item = part.strip().upper()
            if not item or item in seen:
                continue
            seen.add(item)
            parsed.append(item)
        if not parsed:
            raise HTTPException(400, "names must list at least one indicator")
        return parsed
    if name:
        return [name.strip().upper()]
    raise HTTPException(400, "pass name or names")


@app.get("/indicators")
def get_indicators(
    symbol: str = Query(...),
    name: Optional[str] = Query(None, description="e.g. MA_20, RSI_14, MACD_12_26_9"),
    names: Optional[str] = Query(
        None,
        description="Comma-separated series computed in one pass, e.g. MA_5,MA_20,MA_50",
    ),
    timeframe: Timeframe = Query("daily"),
    market: Optional[str] = Query(None),
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    _: None = Depends(_check_key),
):
    """Cached indicator series. Missing or stale names are computed and stored.

    `timeframe=daily|weekly|monthly` uses that bar series (on-demand for W/M and
    for SMA windows outside the daily job). `name` keeps the original single-series
    response; `names` returns a map of series.
    """
    symbol = symbol.strip().upper()
    requested = _indicator_names(name, names)
    market_u = _resolve_market(symbol, market)

    with session() as con:
        try:
            ensure_indicators(con, market_u, symbol, timeframe, requested)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        available = list_indicator_names(con, market_u, symbol, timeframe)
        by_name = {
            item: read_indicator(con, market_u, symbol, timeframe, item, from_, to)
            for item in requested
        }

    if names:
        return {
            "market": market_u,
            "symbol": symbol,
            "timeframe": timeframe,
            "from": from_.isoformat() if from_ else None,
            "to": to.isoformat() if to else None,
            "available_names": available,
            "series": by_name,
        }

    series = by_name[requested[0]]
    return {
        "market": market_u,
        "symbol": symbol,
        "timeframe": timeframe,
        "name": requested[0],
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
        "count": len(series),
        "available_names": available,
        "series": series,
    }


@app.get("/screener/top50")
def screener_top50(
    market: str = Query(..., description="HK or US"),
    on: Optional[date] = Query(None, alias="date", description="Session; default latest"),
    _: None = Depends(_check_key),
):
    """Top-50 turnover pool for one market, plus the spike flag for each name."""
    market_u = market.strip().upper()
    if market_u not in {"US", "HK"}:
        raise HTTPException(400, "market must be HK or US")
    with session() as con:
        try:
            return build_top50(con, market_u, on)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/strategy-board")
def strategy_board(
    market: str = Query("US", description="US only"),
    as_of: Optional[date] = Query(
        None, description="Session the board was scored for; default latest stored night"
    ),
    grid_version: str = Query(GRID_VERSION),
    _: None = Depends(_check_key),
):
    """Top-3 strategies per US Top-50 name from the overnight board."""
    market_u = market.strip().upper()
    if market_u != "US":
        raise HTTPException(400, "strategy board is US Top-50 only")
    with session() as con:
        try:
            return read_board(con, market=market_u, as_of=as_of, grid_version=grid_version)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/strategies")
def strategies_list(owner: str = Depends(require_owner)):
    with session() as con:
        rows = list_strategies(con, owner)
    return {"owner_email": owner, "count": len(rows), "strategies": rows}


@app.post("/strategies")
def strategies_create(body: StrategyBody, owner: str = Depends(require_owner)):
    with session() as con:
        try:
            created = create_strategy(con, owner, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return created


@app.get("/strategies/{strategy_id}")
def strategies_get(strategy_id: str, owner: str = Depends(require_owner)):
    with session() as con:
        row = get_strategy(con, owner, strategy_id)
    if row is None:
        raise HTTPException(404, "Strategy not found")
    return row


@app.put("/strategies/{strategy_id}")
def strategies_update(
    strategy_id: str, body: StrategyBody, owner: str = Depends(require_owner)
):
    with session() as con:
        try:
            row = update_strategy(con, owner, strategy_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "Strategy not found")
    return row


@app.delete("/strategies/{strategy_id}")
def strategies_delete(strategy_id: str, owner: str = Depends(require_owner)):
    with session() as con:
        ok = delete_strategy(con, owner, strategy_id)
    if not ok:
        raise HTTPException(404, "Strategy not found")
    return Response(status_code=204)


@app.post("/strategies/{strategy_id}/backtest")
def strategies_backtest(
    strategy_id: str, body: BacktestBody, owner: str = Depends(require_owner)
):
    """Run a dated backtest. Costs default to the PRD schedule when omitted."""
    if body.start > body.end:
        raise HTTPException(400, "start must be on or before end")
    with session() as con:
        strat = get_strategy(con, owner, strategy_id)
        if strat is None:
            raise HTTPException(404, "Strategy not found")
        try:
            return run_backtest(con, strat, body.start, body.end, body.costs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/")
def root_redirect():
    """Charts UI is the default landing page."""
    return RedirectResponse(url="/ui/")


if STATIC_DIR.is_dir():
    # Rewrites index.html so app.js/styles.css are content-hashed, and sets
    # Cache-Control so the shell and those assets cannot be cached apart.
    app.mount("/ui", UiStaticFiles(directory=STATIC_DIR), name="ui")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "dixon.api:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()

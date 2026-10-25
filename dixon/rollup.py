"""Rebuild weekly & monthly bars from daily using exchange calendars.

Assumptions (see also dixon/calendars.py and indicators/FORMULAS.md):
- Only dates present in daily_bars are rolled (Yahoo gaps stay gaps).
- Weekly: group by ISO week (year-week); open=first session open, high=max,
  low=min, close=last session close, volume=sum; period_start/end = first/last
  trade_date in the group.
- Monthly: group by calendar year-month; same OHLC aggregation.
- We do NOT invent bars for calendar sessions missing from Yahoo.
- After daily restates, re-run this job for affected symbols (or --all).

Usage:
  python -m dixon.rollup --all
  python -m dixon.rollup --symbols AAPL,0700.HK --parquet
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from dixon.calendars import assign_periods, using_fallback
from dixon.config import DB_PATH, PARQUET_ROOT
from dixon.db import connect
from dixon.ingest import export_parquet


def _load_daily(
    con: duckdb.DuckDBPyConnection, market: str, symbol: str
) -> pd.DataFrame:
    return con.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM daily_bars
        WHERE market = ? AND symbol = ?
        ORDER BY trade_date
        """,
        [market, symbol],
    ).fetchdf()


def _aggregate(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "period_start",
                "period_end",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "bar_count",
            ]
        )
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["_period"] = assign_periods(df["trade_date"], kind)
    rows = []
    for _, g in df.groupby("_period", sort=True):
        g = g.sort_values("trade_date")
        rows.append(
            {
                "period_start": g["trade_date"].iloc[0].date(),
                "period_end": g["trade_date"].iloc[-1].date(),
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
                "volume": int(g["volume"].fillna(0).sum()),
                "bar_count": int(len(g)),
            }
        )
    return pd.DataFrame(rows)


def rebuild_symbol(
    con: duckdb.DuckDBPyConnection, market: str, symbol: str
) -> tuple[int, int]:
    daily = _load_daily(con, market, symbol)
    weekly = _aggregate(daily, "weekly")
    monthly = _aggregate(daily, "monthly")

    con.execute(
        "DELETE FROM weekly_bars WHERE market = ? AND symbol = ?", [market, symbol]
    )
    con.execute(
        "DELETE FROM monthly_bars WHERE market = ? AND symbol = ?", [market, symbol]
    )

    if not weekly.empty:
        con.register("_w", weekly.assign(market=market, symbol=symbol))
        con.execute(
            """
            INSERT INTO weekly_bars
              (market, symbol, period_end, period_start, open, high, low, close, volume, bar_count, rebuilt_at)
            SELECT market, symbol, period_end, period_start, open, high, low, close, volume, bar_count, now()
            FROM _w
            """
        )
        con.unregister("_w")

    if not monthly.empty:
        con.register("_m", monthly.assign(market=market, symbol=symbol))
        con.execute(
            """
            INSERT INTO monthly_bars
              (market, symbol, period_end, period_start, open, high, low, close, volume, bar_count, rebuilt_at)
            SELECT market, symbol, period_end, period_start, open, high, low, close, volume, bar_count, now()
            FROM _m
            """
        )
        con.unregister("_m")

    return len(weekly), len(monthly)


def rollup(
    symbols: list[tuple[str, str]] | None = None,
    db_path: Path | None = None,
    write_parquet: bool = False,
) -> dict:
    """symbols: list of (market, symbol); None = all with daily bars."""
    con = connect(db_path)
    run_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO ingest_runs (run_id, job_type, started_at, status)
        VALUES (?, 'rollup', ?, 'running')
        """,
        [run_id, datetime.now()],
    )
    if using_fallback():
        print("WARNING: exchange_calendars fallback active — see dixon/calendars.py")

    try:
        if symbols is None:
            rows = con.execute(
                "SELECT DISTINCT market, symbol FROM daily_bars ORDER BY 1, 2"
            ).fetchall()
            symbols = [(r[0], r[1]) for r in rows]

        summary = []
        for market, symbol in symbols:
            w, m = rebuild_symbol(con, market, symbol)
            print(f"[{market}] {symbol}: weekly={w} monthly={m}")
            summary.append({"market": market, "symbol": symbol, "weekly": w, "monthly": m})

        if write_parquet and summary:
            for mkt in sorted({s["market"] for s in summary}):
                export_parquet(con, mkt, "weekly", PARQUET_ROOT)
                export_parquet(con, mkt, "monthly", PARQUET_ROOT)

        con.execute(
            """
            UPDATE ingest_runs SET finished_at = ?, status = 'ok',
              detail = ? WHERE run_id = ?
            """,
            [datetime.now(), f"symbols={len(summary)}", run_id],
        )
        return {"run_id": run_id, "results": summary}
    except Exception as e:
        con.execute(
            """
            UPDATE ingest_runs SET finished_at = ?, status = 'error', detail = ?
            WHERE run_id = ?
            """,
            [datetime.now(), str(e), run_id],
        )
        raise
    finally:
        con.close()


def main() -> None:
    from dixon.yahoo import infer_market

    p = argparse.ArgumentParser(description="Rebuild weekly/monthly bars")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--all", action="store_true")
    p.add_argument("--symbols", type=str, default="", help="Comma-separated")
    p.add_argument("--parquet", action="store_true")
    args = p.parse_args()

    syms: list[tuple[str, str]] | None
    if args.all or not args.symbols:
        syms = None
    else:
        syms = [(infer_market(s.strip()), s.strip().upper()) for s in args.symbols.split(",") if s.strip()]

    out = rollup(syms, db_path=args.db, write_parquet=args.parquet)
    print(out)


if __name__ == "__main__":
    main()

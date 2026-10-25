"""Compute and cache technical indicators (MA, EMA, RSI, MACD, Bollinger).

Formulas: see indicators/FORMULAS.md — best-effort vs Yahoo math, NOT TradingView.

Usage:
  python -m dixon.compute_indicators --all
  python -m dixon.compute_indicators --symbols AAPL,0700.HK
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from dixon.config import DB_PATH
from dixon.db import connect


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    # adjust=False → recursive EMA matching common Yahoo/Trading platforms seed
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI (smoothed average of gains/losses)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing via ewm alpha=1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 and avg_gain > 0 → RSI = 100
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return rsi


def macd_series(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, window)
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


DEFAULT_SPECS = [
    ("MA_20", "ma", {"window": 20}),
    ("MA_50", "ma", {"window": 50}),
    ("MA_200", "ma", {"window": 200}),
    ("EMA_12", "ema", {"span": 12}),
    ("EMA_26", "ema", {"span": 26}),
    ("RSI_14", "rsi", {"period": 14}),
    ("MACD_12_26_9", "macd", {"fast": 12, "slow": 26, "signal": 9}),
    ("BB_20_2", "bb", {"window": 20, "num_std": 2.0}),
]


def compute_frame(close: pd.Series) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    out["MA_20"] = sma(close, 20)
    out["MA_50"] = sma(close, 50)
    out["MA_200"] = sma(close, 200)
    out["EMA_12"] = ema(close, 12)
    out["EMA_26"] = ema(close, 26)
    out["RSI_14"] = rsi_wilder(close, 14)
    macd_line, signal_line, hist = macd_series(close, 12, 26, 9)
    out["MACD_12_26_9"] = macd_line
    out["MACD_SIGNAL_12_26_9"] = signal_line
    out["MACD_HIST_12_26_9"] = hist
    mid, upper, lower = bollinger(close, 20, 2.0)
    out["BB_MID_20_2"] = mid
    out["BB_UPPER_20_2"] = upper
    out["BB_LOWER_20_2"] = lower
    return out


def write_indicators(
    con: duckdb.DuckDBPyConnection, market: str, symbol: str, frame: pd.DataFrame, series_map: dict[str, pd.Series]
) -> int:
    con.execute(
        "DELETE FROM indicator_cache WHERE market = ? AND symbol = ?",
        [market, symbol],
    )
    rows = []
    dates = pd.to_datetime(frame["trade_date"]).dt.date
    for name, ser in series_map.items():
        for d, v in zip(dates, ser):
            if pd.isna(v):
                continue
            rows.append((market, symbol, d, name, float(v)))
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["market", "symbol", "trade_date", "name", "value"])
    con.register("_ind", df)
    con.execute(
        """
        INSERT INTO indicator_cache (market, symbol, trade_date, name, value, computed_at)
        SELECT market, symbol, trade_date, name, value, now() FROM _ind
        """
    )
    con.unregister("_ind")
    return len(rows)


def compute_symbol(con: duckdb.DuckDBPyConnection, market: str, symbol: str) -> int:
    df = con.execute(
        """
        SELECT trade_date, close
        FROM daily_bars
        WHERE market = ? AND symbol = ?
        ORDER BY trade_date
        """,
        [market, symbol],
    ).fetchdf()
    if df.empty:
        return 0
    close = df["close"].astype(float)
    series_map = compute_frame(close)
    n = write_indicators(con, market, symbol, df, series_map)
    print(f"[{market}] {symbol}: {n} indicator points")
    return n


def compute_all(
    symbols: list[tuple[str, str]] | None = None,
    db_path: Path | None = None,
) -> dict:
    con = connect(db_path)
    run_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO ingest_runs (run_id, job_type, started_at, status)
        VALUES (?, 'indicators', ?, 'running')
        """,
        [run_id, datetime.now()],
    )
    try:
        if symbols is None:
            rows = con.execute(
                "SELECT DISTINCT market, symbol FROM daily_bars ORDER BY 1, 2"
            ).fetchall()
            symbols = [(r[0], r[1]) for r in rows]
        total = 0
        for market, symbol in symbols:
            total += compute_symbol(con, market, symbol)
        con.execute(
            """
            UPDATE ingest_runs SET finished_at = ?, status = 'ok', detail = ?
            WHERE run_id = ?
            """,
            [datetime.now(), f"points={total}", run_id],
        )
        return {"run_id": run_id, "points": total, "symbols": len(symbols)}
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

    p = argparse.ArgumentParser(description="Compute Dixon indicator cache")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--all", action="store_true")
    p.add_argument("--symbols", type=str, default="")
    args = p.parse_args()

    if args.symbols:
        syms = [
            (infer_market(s.strip()), s.strip().upper())
            for s in args.symbols.split(",")
            if s.strip()
        ]
    else:
        syms = None

    print(compute_all(syms, db_path=args.db))


if __name__ == "__main__":
    main()

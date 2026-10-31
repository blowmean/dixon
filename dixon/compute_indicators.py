"""Compute and cache technical indicators (MA, EMA, RSI, MACD, Bollinger).

Formulas: see indicators/FORMULAS.md — best-effort vs Yahoo math, NOT TradingView.

Daily defaults are written by the daily job. Any other series, and weekly or
monthly windows, are computed on first use and reused while the bar fingerprint
matches (`indicator_meta`).

Storage: `indicator_cache.name` is the logical name for daily bars. Weekly and
monthly rows use a prefix (`W|MA_20`, `M|MA_20`) so they do not collide with
daily dates that share a `period_end`.

Usage:
  python -m dixon.compute_indicators --all
  python -m dixon.compute_indicators --symbols AAPL,0700.HK
"""

from __future__ import annotations

import argparse
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from dixon.config import DB_PATH
from dixon.db import connect
from dixon.rules import MA_MENU
from dixon.screener import to_date

TIMEFRAMES = ("daily", "weekly", "monthly")
_TF_PREFIX = {"daily": "", "weekly": "W|", "monthly": "M|"}


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def volume_sma(volume: pd.Series, window: int) -> pd.Series:
    """SMA of volume ending on the current bar (the signal bar is included)."""
    return sma(pd.to_numeric(volume, errors="coerce"), window)


def prior_high(high: pd.Series, window: int) -> pd.Series:
    """Max high of the previous `window` bars, excluding the current bar."""
    return pd.to_numeric(high, errors="coerce").shift(1).rolling(window, min_periods=window).max()


def prior_low(low: pd.Series, window: int) -> pd.Series:
    """Min low of the previous `window` bars, excluding the current bar."""
    return pd.to_numeric(low, errors="coerce").shift(1).rolling(window, min_periods=window).min()


def rate_of_change(close: pd.Series, period: int) -> pd.Series:
    """ROC in percent points: (close / close_n_bars_ago − 1) × 100.

    The signal bar is the current close. The reference close is `period` bars
    earlier, so the first `period` values are NaN. A non-positive reference
    close is NaN (the ratio is undefined).
    """
    px = pd.to_numeric(close, errors="coerce")
    prev = px.shift(period)
    roc = (px / prev - 1.0) * 100.0
    return roc.mask(prev <= 0)


def series_by_date(dates: pd.Series, values: pd.Series) -> dict:
    """Map each bar date to a finite value. Warm-up NaNs are omitted."""
    out: dict = {}
    for d, v in zip(dates.tolist(), values.tolist()):
        if v is None or pd.isna(v):
            continue
        out[to_date(d)] = float(v)
    return out


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
    ("MA_5", "ma", {"window": 5}),
    ("MA_9", "ma", {"window": 9}),
    ("MA_20", "ma", {"window": 20}),
    ("MA_30", "ma", {"window": 30}),
    ("MA_50", "ma", {"window": 50}),
    ("MA_100", "ma", {"window": 100}),
    ("MA_200", "ma", {"window": 200}),
    ("MA_250", "ma", {"window": 250}),
    ("EMA_12", "ema", {"span": 12}),
    ("EMA_26", "ema", {"span": 26}),
    ("RSI_14", "rsi", {"period": 14}),
    ("MACD_12_26_9", "macd", {"fast": 12, "slow": 26, "signal": 9}),
    ("BB_20_2", "bb", {"window": 20, "num_std": 2.0}),
]


def compute_frame(close: pd.Series) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for window in (*MA_MENU, 200):
        out[f"MA_{window}"] = sma(close, window)
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


def storage_name(timeframe: str, name: str) -> str:
    if timeframe not in _TF_PREFIX:
        raise ValueError("timeframe must be daily, weekly, or monthly")
    return _TF_PREFIX[timeframe] + name


def logical_name(stored: str) -> str:
    if stored.startswith("W|") or stored.startswith("M|"):
        return stored[2:]
    return stored


def timeframe_of_stored(stored: str) -> str:
    if stored.startswith("W|"):
        return "weekly"
    if stored.startswith("M|"):
        return "monthly"
    return "daily"


_INDICATOR_NAME = re.compile(
    r"^(?:MA_\d+|EMA_\d+|RSI_\d+"
    r"|MACD_\d+_\d+_\d+|MACD_SIGNAL_\d+_\d+_\d+|MACD_HIST_\d+_\d+_\d+"
    r"|BB_(?:MID|UPPER|LOWER)_\d+_(?:\d+(?:\.\d+)?))$"
)


def assert_indicator_name(name: str) -> str:
    """Reject unknown series names without computing them."""
    cleaned = name.strip().upper()
    if not _INDICATOR_NAME.match(cleaned):
        raise ValueError(f"unknown indicator {cleaned}")
    return cleaned


def compute_named(close: pd.Series, name: str) -> pd.Series:
    """One logical series from a close column. Raises ValueError if unknown."""
    name = assert_indicator_name(name)
    m = re.fullmatch(r"MA_(\d+)", name)
    if m:
        return sma(close, int(m.group(1)))
    m = re.fullmatch(r"EMA_(\d+)", name)
    if m:
        return ema(close, int(m.group(1)))
    m = re.fullmatch(r"RSI_(\d+)", name)
    if m:
        return rsi_wilder(close, int(m.group(1)))
    m = re.fullmatch(r"MACD_(\d+)_(\d+)_(\d+)", name)
    if m:
        line, _sig, _hist = macd_series(close, int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return line
    m = re.fullmatch(r"MACD_SIGNAL_(\d+)_(\d+)_(\d+)", name)
    if m:
        _line, sig, _hist = macd_series(close, int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return sig
    m = re.fullmatch(r"MACD_HIST_(\d+)_(\d+)_(\d+)", name)
    if m:
        _line, _sig, hist = macd_series(close, int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return hist
    m = re.fullmatch(r"BB_(MID|UPPER|LOWER)_(\d+)_(\d+(?:\.\d+)?)", name)
    if m:
        mid, upper, lower = bollinger(close, int(m.group(2)), float(m.group(3)))
        return {"MID": mid, "UPPER": upper, "LOWER": lower}[m.group(1)]
    raise ValueError(f"unknown indicator {name}")


def load_ohlc(
    con: duckdb.DuckDBPyConnection, market: str, symbol: str, timeframe: str
) -> pd.DataFrame:
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe must be daily, weekly, or monthly")
    table = {"daily": "daily_bars", "weekly": "weekly_bars", "monthly": "monthly_bars"}[timeframe]
    date_col = "trade_date" if timeframe == "daily" else "period_end"
    df = con.execute(
        f"""
        SELECT {date_col} AS bar_date, open, high, low, close, volume
        FROM {table}
        WHERE market = ? AND symbol = ?
        ORDER BY {date_col}
        """,
        [market, symbol],
    ).fetchdf()
    if df.empty:
        return df
    df["bar_date"] = df["bar_date"].map(to_date)
    df["close"] = df["close"].astype(float)
    return df


def bars_fingerprint(df: pd.DataFrame) -> str:
    closes = df["close"].astype(float)
    last = to_date(df.iloc[-1]["bar_date"])
    return f"{len(df)}|{last.isoformat()}|{float(closes.sum()):.8f}|{float(closes.iloc[-1]):.8f}"


def _store_series(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    series_map: dict[str, pd.Series],
    fingerprint: str,
) -> int:
    """Replace the named series for this symbol/timeframe. Other names are kept."""
    if not series_map:
        return 0
    stored_names = [storage_name(timeframe, name) for name in series_map]
    placeholders = ", ".join("?" * len(stored_names))
    con.execute(
        f"""
        DELETE FROM indicator_cache
        WHERE market = ? AND symbol = ? AND name IN ({placeholders})
        """,
        [market, symbol, *stored_names],
    )
    rows: list[tuple[Any, ...]] = []
    dates = [to_date(d) for d in frame["bar_date"]]
    for name, ser in series_map.items():
        stored = storage_name(timeframe, name)
        for d, v in zip(dates, ser.to_numpy()):
            if v is None or pd.isna(v):
                continue
            rows.append((market, symbol, d, stored, float(v)))
    if rows:
        df = pd.DataFrame(rows, columns=["market", "symbol", "trade_date", "name", "value"])
        con.register("_ind", df)
        con.execute(
            """
            INSERT INTO indicator_cache (market, symbol, trade_date, name, value, computed_at)
            SELECT market, symbol, trade_date, name, value, now() FROM _ind
            """
        )
        con.unregister("_ind")
    for name in series_map:
        con.execute(
            """
            INSERT OR REPLACE INTO indicator_meta
              (market, symbol, timeframe, name, fingerprint, updated_at)
            VALUES (?, ?, ?, ?, ?, now())
            """,
            [market, symbol, timeframe, name, fingerprint],
        )
    return len(rows)


def write_indicators(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    frame: pd.DataFrame,
    series_map: dict[str, pd.Series],
) -> int:
    """Daily-cache writer. Replaces only the names in `series_map` (other timeframes stay)."""
    work = frame.copy()
    if "bar_date" not in work.columns:
        if "trade_date" not in work.columns or "close" not in work.columns:
            raise ValueError("frame must include trade_date and close")
        work = work.rename(columns={"trade_date": "bar_date"})
    work["bar_date"] = work["bar_date"].map(to_date)
    work["close"] = work["close"].astype(float)
    return _store_series(con, market, symbol, "daily", work, series_map, bars_fingerprint(work))


def ensure_indicators(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    timeframe: str,
    names: list[str],
) -> None:
    """Compute any requested series whose cached fingerprint is missing or stale."""
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe must be daily, weekly, or monthly")
    logical = [assert_indicator_name(n) for n in names if n and str(n).strip()]
    logical = list(dict.fromkeys(logical))
    if not logical:
        return

    df = load_ohlc(con, market, symbol, timeframe)
    if df.empty:
        return
    fp = bars_fingerprint(df)
    stale: list[str] = []
    for name in logical:
        meta = con.execute(
            """
            SELECT fingerprint FROM indicator_meta
            WHERE market = ? AND symbol = ? AND timeframe = ? AND name = ?
            """,
            [market, symbol, timeframe, name],
        ).fetchone()
        stored = storage_name(timeframe, name)
        cached = con.execute(
            """
            SELECT 1 FROM indicator_cache
            WHERE market = ? AND symbol = ? AND name = ?
            LIMIT 1
            """,
            [market, symbol, stored],
        ).fetchone()
        if meta is None or meta[0] != fp or cached is None:
            stale.append(name)
    if not stale:
        return
    close = df["close"].astype(float)
    series_map = {name: compute_named(close, name) for name in stale}
    _store_series(con, market, symbol, timeframe, df, series_map, fp)


def read_indicator(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    timeframe: str,
    name: str,
    from_: Any = None,
    to: Any = None,
) -> list[dict[str, Any]]:
    stored = storage_name(timeframe, name.strip().upper())
    clauses = ["market = ?", "symbol = ?", "name = ?"]
    params: list[Any] = [market, symbol, stored]
    if from_ is not None:
        clauses.append("trade_date >= ?")
        params.append(from_)
    if to is not None:
        clauses.append("trade_date <= ?")
        params.append(to)
    rows = con.execute(
        f"""
        SELECT trade_date AS date, value
        FROM indicator_cache
        WHERE {' AND '.join(clauses)}
        ORDER BY trade_date
        """,
        params,
    ).fetchall()
    out = []
    for d, value in rows:
        out.append({"date": to_date(d).isoformat(), "value": None if value is None else float(value)})
    return out


def list_indicator_names(
    con: duckdb.DuckDBPyConnection, market: str, symbol: str, timeframe: str
) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT name FROM indicator_cache
        WHERE market = ? AND symbol = ?
        ORDER BY 1
        """,
        [market, symbol],
    ).fetchall()
    names = [logical_name(r[0]) for r in rows if timeframe_of_stored(r[0]) == timeframe]
    return sorted(set(names))


def indicator_map(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    timeframe: str,
    name: str,
) -> dict:
    """date → value for a cached series (caller should ensure_indicators first)."""
    from datetime import date as date_cls

    stored = storage_name(timeframe, name.strip().upper())
    rows = con.execute(
        """
        SELECT trade_date, value FROM indicator_cache
        WHERE market = ? AND symbol = ? AND name = ?
        """,
        [market, symbol, stored],
    ).fetchall()
    out: dict[date_cls, float] = {}
    for d, value in rows:
        if value is None:
            continue
        out[to_date(d)] = float(value)
    return out


def compute_symbol(con: duckdb.DuckDBPyConnection, market: str, symbol: str) -> int:
    df = load_ohlc(con, market, symbol, "daily")
    if df.empty:
        return 0
    close = df["close"].astype(float)
    series_map = compute_frame(close)
    n = _store_series(con, market, symbol, "daily", df, series_map, bars_fingerprint(df))
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

"""Top-50 turnover screener (separate HK and US pools).

Turnover = close × volume in local currency (no FX merge).

A name spikes only when it is inside that session's Top-50 and all of:
  close > prior 5-session high (max high of the previous 5 bars),
  day gain (close / previous close − 1) ≥ 5%,
  SMA(30) rising (SMA30_t > SMA30_{t-1}).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import duckdb
import pandas as pd

POOL_SIZE = 50
LOOKBACK_DAYS = 180  # calendar days before the first ranked session; covers SMA30 + 5-day high

_WINDOW_SQL = """
WITH scoped AS (
  SELECT
    symbol,
    trade_date,
    close,
    high,
    volume,
    close * COALESCE(volume, 0) AS turnover
  FROM daily_bars
  WHERE market = ?
    AND trade_date >= ?
    AND trade_date <= ?
),
windowed AS (
  SELECT
    symbol,
    trade_date,
    close,
    high,
    volume,
    turnover,
    LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
    MAX(high) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    ) AS prior_5d_high,
    COUNT(*) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    ) AS n_prior5,
    AVG(close) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
    ) AS sma30,
    COUNT(*) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
    ) AS n_sma30,
    AVG(close) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
    ) AS sma30_prev,
    COUNT(*) OVER (
      PARTITION BY symbol ORDER BY trade_date
      ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
    ) AS n_sma30_prev
  FROM scoped
)
"""


def to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    return date.fromisoformat(str(value)[:10])


def _f(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return float(value)


def _i(value: Any) -> int:
    if value is None:
        return 0
    try:
        if pd.isna(value):
            return 0
    except TypeError:
        pass
    return int(value)


def is_spike(row: dict[str, Any]) -> bool:
    """Spike predicate. `row` uses the windowed column names."""
    prior = _f(row.get("prior_5d_high"))
    close = _f(row.get("close"))
    prev = _f(row.get("prev_close"))
    sma = _f(row.get("sma30"))
    sma_prev = _f(row.get("sma30_prev"))
    if close is None or prior is None or _i(row.get("n_prior5")) < 5:
        return False
    if close <= prior:
        return False
    if prev is None or prev <= 0:
        return False
    if (close / prev - 1.0) < 0.05 - 1e-12:
        return False
    if sma is None or sma_prev is None:
        return False
    if _i(row.get("n_sma30")) < 30 or _i(row.get("n_sma30_prev")) < 30:
        return False
    return sma > sma_prev


def latest_trade_date(con: duckdb.DuckDBPyConnection, market: str) -> date | None:
    row = con.execute(
        "SELECT MAX(trade_date) FROM daily_bars WHERE market = ?",
        [market],
    ).fetchone()
    if not row or row[0] is None:
        return None
    return to_date(row[0])


def _windowed_frame(
    con: duckdb.DuckDBPyConnection,
    market: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    lookback = start - timedelta(days=LOOKBACK_DAYS)
    sql = _WINDOW_SQL + " SELECT * FROM windowed WHERE trade_date >= ? AND trade_date <= ?"
    df = con.execute(sql, [market, lookback, end, start, end]).fetchdf()
    if df.empty:
        return df
    df["trade_date"] = df["trade_date"].map(to_date)
    return df


def _rank_day(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.sort_values(["turnover", "symbol"], ascending=[False, True], kind="mergesort")
    out = out.reset_index(drop=True)
    out["rnk"] = out.index + 1
    return out


def build_top50(
    con: duckdb.DuckDBPyConnection,
    market: str,
    trade_date: date | None = None,
) -> dict[str, Any]:
    """Top-50 pool for one market and session, with spike flags."""
    market = market.upper()
    if market not in {"US", "HK"}:
        raise ValueError("market must be US or HK")
    resolved = trade_date or latest_trade_date(con, market)
    if resolved is None:
        return {
            "market": market,
            "date": None,
            "count": 0,
            "spike_count": 0,
            "rule": _RULE_TEXT,
            "rows": [],
        }
    frame = _windowed_frame(con, market, resolved, resolved)
    day = frame[frame["trade_date"] == resolved] if not frame.empty else frame
    ranked = _rank_day(day).head(POOL_SIZE)
    rows = [_public_row(rec) for rec in ranked.to_dict(orient="records")]
    return {
        "market": market,
        "date": resolved.isoformat(),
        "count": len(rows),
        "spike_count": sum(1 for r in rows if r["spike"]),
        "rule": _RULE_TEXT,
        "rows": rows,
    }


def spike_dates(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> set[date]:
    """Sessions in [start, end] where `symbol` is inside the Top-50 and spikes."""
    frame = _windowed_frame(con, market, start, end)
    if frame.empty:
        return set()
    symbol = symbol.upper()
    hits: set[date] = set()
    for trade_date, day in frame.groupby("trade_date", sort=False):
        ranked = _rank_day(day)
        mine = ranked[ranked["symbol"] == symbol]
        if mine.empty:
            continue
        rec = mine.iloc[0].to_dict()
        if int(rec["rnk"]) <= POOL_SIZE and is_spike(rec):
            hits.add(to_date(trade_date))
    return hits


_RULE_TEXT = (
    "Top 50 by turnover (close×volume, local currency) inside the market. "
    "Spike: close > prior 5-session high AND day gain ≥ 5% AND SMA30 rising."
)


def _public_row(rec: dict[str, Any]) -> dict[str, Any]:
    close = _f(rec.get("close"))
    prev = _f(rec.get("prev_close"))
    gain = None
    if close is not None and prev is not None and prev > 0:
        gain = close / prev - 1.0
    spike = is_spike(rec)
    volume = rec.get("volume")
    vol_out: int | None
    if volume is None or (isinstance(volume, float) and pd.isna(volume)):
        vol_out = None
    else:
        vol_out = int(volume)
    return {
        "rank": int(rec["rnk"]),
        "symbol": rec["symbol"],
        "close": close,
        "volume": vol_out,
        "turnover": _f(rec.get("turnover")),
        "day_gain": gain,
        "prior_5d_high": _f(rec.get("prior_5d_high")),
        "sma30": _f(rec.get("sma30")),
        "sma30_prev": _f(rec.get("sma30_prev")),
        "spike": spike,
    }

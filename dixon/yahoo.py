"""Yahoo Finance download helpers with rate limiting.

Uses yfinance with auto_adjust=True so returned OHLC is split/dividend-adjusted.
We store those adjusted OHLC columns directly (no separate adj_close).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import pandas as pd

from dixon.config import YAHOO_MAX_RETRIES, YAHOO_SLEEP_SEC


def infer_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith(".HK"):
        return "HK"
    return "US"


def infer_asset_class(symbol: str) -> str:
    # Very light heuristic; Day-1 treats SPY as etf, others equity
    etfs = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "2800.HK", "2828.HK"}
    return "etf" if symbol.upper() in etfs else "equity"


def download_adjusted_daily(
    symbol: str,
    start: date,
    end: date,
    sleep_sec: float | None = None,
    max_retries: int | None = None,
) -> pd.DataFrame:
    """Download adjusted daily OHLC for one symbol.

    Returns DataFrame columns: trade_date, open, high, low, close, volume
    Empty DataFrame if Yahoo returns nothing.
    """
    import yfinance as yf

    sleep = YAHOO_SLEEP_SEC if sleep_sec is None else sleep_sec
    retries = YAHOO_MAX_RETRIES if max_retries is None else max_retries
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            # end is exclusive in yfinance; bump by 1 day
            end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)
            ticker = yf.Ticker(symbol)
            df = ticker.history(
                start=str(start),
                end=str(end_excl.date()),
                auto_adjust=True,
                actions=False,
            )
            time.sleep(sleep)
            if df is None or df.empty:
                return pd.DataFrame(
                    columns=["trade_date", "open", "high", "low", "close", "volume"]
                )
            df = df.reset_index()
            # Column may be Date or Datetime
            date_col = "Date" if "Date" in df.columns else df.columns[0]
            out = pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(df[date_col]).dt.tz_localize(None).dt.date,
                    "open": pd.to_numeric(df["Open"], errors="coerce"),
                    "high": pd.to_numeric(df["High"], errors="coerce"),
                    "low": pd.to_numeric(df["Low"], errors="coerce"),
                    "close": pd.to_numeric(df["Close"], errors="coerce"),
                    "volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("int64"),
                }
            )
            # Yahoo sometimes emits placeholder rows with NaN OHLC (esp. HK) —
            # drop them so NOT NULL constraints on daily_bars succeed.
            before = len(out)
            out = out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
            if before and out.empty:
                return out
            return out
        except Exception as e:  # noqa: BLE001 — retry Yahoo flakiness
            last_err = e
            time.sleep(sleep * attempt)

    raise RuntimeError(f"Yahoo download failed for {symbol}: {last_err}")


def fetch_meta(symbol: str) -> dict[str, Any]:
    """Best-effort name/currency/exchange from Yahoo."""
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).fast_info
        # fast_info is attribute-like
        currency = getattr(info, "currency", None) or None
        exchange = getattr(info, "exchange", None) or None
        return {"currency": currency, "exchange": exchange, "name": None}
    except Exception:  # noqa: BLE001
        return {"currency": None, "exchange": None, "name": None}

"""Exchange trading calendars for US (NYSE) and HK (HKEX).

Assumptions (documented for QA):
- US equities use XNYS (NYSE) sessions, including early closes.
- HK equities use XHKG (HKEX) sessions, including half-day sessions.
- Weekly bar: ISO week grouping of sessions that actually traded; period_end =
  last session date in that week that has a daily bar (not calendar Friday if
  that Friday is a holiday / half-day with no bar).
- Monthly bar: calendar month; period_end = last session date in that month
  that has a daily bar.
- Half-days / early closes still produce a daily bar when Yahoo publishes one;
  we do not drop them. Volume may be lower; OHLC still rolls into W/M.
- If exchange_calendars is unavailable, fall back to pandas business-day
  grouping with a clear warning (holidays will be wrong).

Fallback mode is detectable via `using_fallback()`.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Iterable

import pandas as pd

from dixon.config import CALENDAR_BY_MARKET

_FALLBACK = False

try:
    import exchange_calendars as xcals

    _HAS_XCALS = True
except ImportError:  # pragma: no cover
    xcals = None  # type: ignore
    _HAS_XCALS = False


def using_fallback() -> bool:
    return _FALLBACK or not _HAS_XCALS


@lru_cache(maxsize=4)
def _calendar(market: str):
    global _FALLBACK
    market = market.upper()
    name = CALENDAR_BY_MARKET.get(market)
    if not name:
        raise ValueError(f"Unknown market: {market}")
    if not _HAS_XCALS:
        _FALLBACK = True
        warnings.warn(
            "exchange_calendars not installed; using Mon-Fri fallback "
            "(HK holidays / US early-close days will be wrong).",
            stacklevel=2,
        )
        return None
    return xcals.get_calendar(name)


def session_dates(market: str, start: date, end: date) -> pd.DatetimeIndex:
    """Return exchange session dates in [start, end] inclusive."""
    cal = _calendar(market)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if cal is None:
        # Mon-Fri fallback
        idx = pd.bdate_range(start_ts, end_ts, freq="C")
        return idx
    # exchange_calendars uses tz-aware midnight; normalize to dates
    sessions = cal.sessions_in_range(start_ts, end_ts)
    return pd.DatetimeIndex(sessions.tz_localize(None) if sessions.tz is not None else sessions)


def is_session(market: str, d: date) -> bool:
    sessions = session_dates(market, d, d)
    return len(sessions) > 0


def week_key(d: pd.Timestamp | date) -> tuple[int, int]:
    """ISO year + ISO week number."""
    ts = pd.Timestamp(d)
    iso = ts.isocalendar()
    return int(iso.year), int(iso.week)


def month_key(d: pd.Timestamp | date) -> tuple[int, int]:
    ts = pd.Timestamp(d)
    return int(ts.year), int(ts.month)


def assign_periods(trade_dates: Iterable[date | pd.Timestamp], kind: str) -> pd.Series:
    """Map each trade_date to a period label string for grouping.

    kind: 'weekly' -> 'YYYY-Www' (ISO), 'monthly' -> 'YYYY-MM'
    """
    s = pd.to_datetime(pd.Series(list(trade_dates)))
    if kind == "weekly":
        # ISO week label
        return s.map(lambda t: f"{t.isocalendar().year}-W{t.isocalendar().week:02d}")
    if kind == "monthly":
        return s.dt.strftime("%Y-%m")
    raise ValueError(kind)


def default_history_window(years: int = 5) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=int(years * 365.25) + 7)
    return start, end


def ensure_date(d) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    return pd.Timestamp(d).date()

"""Overnight book: buy the close, sell the next open.

This is not the next-open backtest. `dixon.backtest.run_backtest` dispatches
here only when `rules.book` is ``overnight``, and that shape is legal only
for a single long `consec_down_n`. RSI, relative volume, Donchian, ROC, EMA,
Double Seven, and the RSI-slope rules never enter this module.

Public lock: N=3, daily SPY and QQQ, US v1.2 costs when the caller omits
overrides. A signal on session t (the Nth consecutive down close) buys t's
close and sells the next stored session's open. A signal on the last
in-range bar does not open a position, because there is no next open to sell.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

from dixon.backtest import Portfolio, _result
from dixon.compute_indicators import consec_down_flags, load_ohlc, series_by_date
from dixon.costs import cost_model_from_overrides
from dixon.rules import OVERNIGHT_BOOK, validate_rules
from dixon.screener import to_date

OVERNIGHT_SYMBOLS = frozenset({"SPY", "QQQ"})
_FILL = "close_to_next_open"


def assert_overnight_universe(symbol: str, market: str, timeframe: str) -> None:
    """SPY and QQQ, daily US bars. Anything else is not this book."""
    if (
        timeframe != "daily"
        or market != "US"
        or str(symbol).strip().upper() not in OVERNIGHT_SYMBOLS
    ):
        raise ValueError("overnight book is daily SPY and QQQ only")


def run_overnight_book(
    con: duckdb.DuckDBPyConnection,
    strategy: dict[str, Any],
    start: date,
    end: date,
    cost_overrides: dict | None = None,
) -> dict[str, Any]:
    """One-night holds. Entry price is the signal close. Exit price is the next open."""
    if start > end:
        raise ValueError("start must be on or before end")
    rules = validate_rules(strategy["rules"])
    if rules.get("book") != OVERNIGHT_BOOK:
        raise ValueError("overnight book requires book overnight")
    symbol = str(strategy["symbol"]).strip().upper()
    market = str(strategy["market"])
    timeframe = str(strategy["timeframe"])
    assert_overnight_universe(symbol, market, timeframe)
    scoped = {
        **strategy,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "rules": rules,
    }

    n = int(rules["long"]["conditions"][0]["n"])
    costs = cost_model_from_overrides(cost_overrides)
    capital = costs.capital_for(market)
    bars = load_ohlc(con, market, symbol, timeframe)
    if not bars.empty:
        in_range = bars[(bars["bar_date"] >= start) & (bars["bar_date"] <= end)]
        down_map = series_by_date(bars["bar_date"], consec_down_flags(bars["close"], n))
    else:
        in_range = bars
        down_map = {}

    book = Portfolio(cash=capital, costs=costs, market=market)
    curve: list[dict[str, Any]] = []
    if in_range.empty:
        return _tag(_result(scoped, start, end, costs, capital, book, curve, None))

    records = in_range.to_dict(orient="records")
    for i, rec in enumerate(records):
        bar_date = to_date(rec["bar_date"])
        open_px = float(rec["open"])
        close_px = float(rec["close"])
        # Yesterday's close entry, if any, leaves at this open. The same bar
        # can then open a new overnight at its own close.
        if symbol in book.positions:
            book.close_position(symbol, open_px, bar_date, "overnight")
        if down_map.get(bar_date) == 1.0 and i + 1 < len(records):
            book.try_open(symbol, "long", close_px, bar_date, {symbol: close_px})

        mark_map = {symbol: close_px}
        for sym, pos in book.positions.items():
            mark_map.setdefault(sym, pos.entry_price)
        curve.append(
            {
                "date": bar_date.isoformat(),
                "equity": book.equity(mark_map),
                "cash": book.cash,
                "side": book.side_of(symbol),
            }
        )

    open_pos = book.positions.get(symbol)
    open_payload = None
    if open_pos is not None:
        last_close = float(records[-1]["close"])
        unreal = (last_close - open_pos.entry_price) * open_pos.shares
        unreal -= open_pos.entry_commission + open_pos.entry_slippage
        open_payload = {
            "symbol": symbol,
            "side": open_pos.side,
            "shares": open_pos.shares,
            "entry_date": open_pos.entry_date.isoformat(),
            "entry_price": open_pos.entry_price,
            "notional": open_pos.notional,
            "mark": last_close,
            "unrealized_pnl": unreal,
        }
    return _tag(_result(scoped, start, end, costs, capital, book, curve, open_payload))


def _tag(payload: dict[str, Any]) -> dict[str, Any]:
    """Mark the result so a next-open payload cannot be mistaken for this book."""
    payload["book"] = OVERNIGHT_BOOK
    payload["fill"] = _FILL
    return payload

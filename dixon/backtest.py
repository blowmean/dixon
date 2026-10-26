"""Next-open backtest for one owner strategy (PRD v1.2).

Accounting:
  * Start flat, in the symbol's currency (HKD 500_000 / USD 100_000 by default).
  * A signal on session t fills at the next stored session's open.
  * Each entry targets `equity_fraction` (default 20%) of marked equity.
  * Gross entry notional across open positions cannot exceed equity, and the
    book refuses a 6th concurrent name. A strategy has one symbol, so a single
    run holds at most one position; the cap is enforced in the book anyway.
  * Long cash outlay is notional + commission + slippage. If cash is short of
    the 20% ticket, the fill is scaled down. Below 1 unit of currency, skip.
  * Shorts are synthetic (no borrow). Cash is credited the sale proceeds;
    entry notional stays in the gross-exposure sum so later tickets still see it.
  * Open positions are marked at each session close. The test does not invent
    a flatten on the end date; `open_position` carries unrealized P&L.
  * Dividends are ignored. P&L is price change minus the costs above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from dixon.compute_indicators import (
    ensure_indicators,
    indicator_map,
    load_ohlc,
    prior_high,
    prior_low,
    series_by_date,
    volume_sma,
)
from dixon.costs import CostModel, commission, cost_model_from_overrides, slip_per_share
from dixon.rules import SignalView, signal_requirements, target_side, validate_rules, windows_used
from dixon.screener import spike_dates, to_date

MAX_CONCURRENT = 5
WIN_RATE_TARGET = 0.50
MAX_DD_TARGET = 0.10


@dataclass
class Position:
    symbol: str
    side: str
    shares: float
    entry_price: float
    entry_date: date
    entry_commission: float
    entry_slippage: float

    @property
    def notional(self) -> float:
        return self.shares * self.entry_price


@dataclass
class Portfolio:
    cash: float
    costs: CostModel
    market: str
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def side_of(self, symbol: str) -> str:
        pos = self.positions.get(symbol)
        return pos.side if pos else "flat"

    def locked_notional(self) -> float:
        return sum(p.notional for p in self.positions.values())

    def equity(self, marks: dict[str, float]) -> float:
        eq = self.cash
        for sym, pos in self.positions.items():
            mark = float(marks[sym])
            if pos.side == "long":
                eq += pos.shares * mark
            else:
                eq -= pos.shares * mark
        return eq

    def _fill_costs(self, shares: float, price: float) -> tuple[float, float]:
        comm = commission(self.market, shares, price, self.costs)
        slip = shares * slip_per_share(self.market, price, self.costs)
        return comm, slip

    def _max_long_shares(self, price: float) -> float:
        if self.cash <= 0 or price <= 0:
            return 0.0
        lo, hi = 0.0, self.cash / price
        for _ in range(48):
            mid = (lo + hi) / 2.0
            comm, slip = self._fill_costs(mid, price)
            if mid * price + comm + slip <= self.cash + 1e-8:
                lo = mid
            else:
                hi = mid
        return lo

    def close_position(self, symbol: str, price: float, when: date, reason: str) -> None:
        pos = self.positions.pop(symbol)
        comm, slip = self._fill_costs(pos.shares, price)
        if pos.side == "long":
            self.cash += pos.shares * price - comm - slip
            price_pnl = (price - pos.entry_price) * pos.shares
        else:
            self.cash -= pos.shares * price + comm + slip
            price_pnl = (pos.entry_price - price) * pos.shares
        pnl = price_pnl - pos.entry_commission - pos.entry_slippage - comm - slip
        self.trades.append(
            {
                "symbol": symbol,
                "side": pos.side,
                "entry_date": pos.entry_date.isoformat(),
                "entry_price": pos.entry_price,
                "exit_date": when.isoformat(),
                "exit_price": price,
                "shares": pos.shares,
                "notional": pos.notional,
                "pnl": pnl,
                "return_on_notional": (pnl / pos.notional) if pos.notional else None,
                "entry_commission": pos.entry_commission,
                "exit_commission": comm,
                "entry_slippage": pos.entry_slippage,
                "exit_slippage": slip,
                "exit_reason": reason,
            }
        )

    def try_open(
        self,
        symbol: str,
        side: str,
        price: float,
        when: date,
        marks: dict[str, float],
    ) -> bool:
        if side not in {"long", "short"} or symbol in self.positions:
            return False
        if len(self.positions) >= MAX_CONCURRENT:
            return False
        if price <= 0:
            return False
        eq = self.equity(marks)
        if eq <= 0:
            return False
        room = eq - self.locked_notional()
        notional = min(self.costs.equity_fraction * eq, room)
        if notional < 1.0:
            return False
        shares = notional / price
        if side == "long":
            shares = min(shares, self._max_long_shares(price))
        if shares <= 0 or shares * price < 1.0:
            return False
        comm, slip = self._fill_costs(shares, price)
        if side == "long":
            need = shares * price + comm + slip
            if need > self.cash + 1e-6:
                return False
            self.cash -= need
        else:
            if comm + slip > self.cash + 1e-6:
                return False
            self.cash += shares * price - comm - slip
        self.positions[symbol] = Position(
            symbol=symbol,
            side=side,
            shares=shares,
            entry_price=price,
            entry_date=when,
            entry_commission=comm,
            entry_slippage=slip,
        )
        return True

    def set_target(
        self,
        symbol: str,
        target: str,
        price: float,
        when: date,
        marks: dict[str, float],
    ) -> None:
        current = self.side_of(symbol)
        if current == target:
            return
        if current != "flat":
            self.close_position(symbol, price, when, "signal")
        if target in {"long", "short"}:
            # Revalue after the close. The closed name is flat; other names use `marks`.
            self.try_open(symbol, target, price, when, marks)


def _round_money(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _bar_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _max_drawdown(equities: list[float]) -> float:
    if not equities:
        return 0.0
    peak = equities[0]
    worst = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            worst = max(worst, (peak - eq) / peak)
    return worst


def run_backtest(
    con: duckdb.DuckDBPyConnection,
    strategy: dict[str, Any],
    start: date,
    end: date,
    cost_overrides: dict | None = None,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must be on or before end")
    rules = validate_rules(strategy["rules"])
    market = strategy["market"]
    symbol = strategy["symbol"]
    timeframe = strategy["timeframe"]
    costs = cost_model_from_overrides(cost_overrides)
    capital = costs.capital_for(market)

    windows = windows_used(rules)
    reqs = signal_requirements(rules)
    indicator_names = [f"MA_{w}" for w in windows]
    indicator_names.extend(f"RSI_{period}" for period in reqs["rsi_periods"])
    if indicator_names:
        ensure_indicators(con, market, symbol, timeframe, indicator_names)
    ma_maps = {
        w: indicator_map(con, market, symbol, timeframe, f"MA_{w}") for w in windows
    }
    rsi_maps = {
        period: indicator_map(con, market, symbol, timeframe, f"RSI_{period}")
        for period in reqs["rsi_periods"]
    }

    bars = load_ohlc(con, symbol=symbol, market=market, timeframe=timeframe)
    if not bars.empty:
        in_range = bars[(bars["bar_date"] >= start) & (bars["bar_date"] <= end)]
    else:
        in_range = bars

    vol_maps = {
        w: series_by_date(bars["bar_date"], volume_sma(bars["volume"], w))
        for w in reqs["volume_windows"]
    } if not bars.empty else {}
    high_maps = {
        w: series_by_date(bars["bar_date"], prior_high(bars["high"], w))
        for w in reqs["donchian_high_windows"]
    } if not bars.empty else {}
    low_maps = {
        w: series_by_date(bars["bar_date"], prior_low(bars["low"], w))
        for w in reqs["donchian_low_windows"]
    } if not bars.empty else {}

    spikes: set[date] = set()
    if reqs["needs_spike"]:
        spikes = spike_dates(con, market, symbol, start, end)

    book = Portfolio(cash=capital, costs=costs, market=market)
    curve: list[dict[str, Any]] = []

    if in_range.empty:
        return _result(strategy, start, end, costs, capital, book, curve, None)

    records = in_range.to_dict(orient="records")
    # Previous-bar MAs may sit just before `start` (warmup).
    first_pos = int(in_range.index[0])
    if first_pos > 0:
        prev_date = to_date(bars.iloc[first_pos - 1]["bar_date"])
        mas_prev = {w: ma_maps[w].get(prev_date) for w in windows}
    else:
        mas_prev = {w: None for w in windows}

    pending: str | None = None
    for i, rec in enumerate(records):
        bar_date = to_date(rec["bar_date"])
        open_px = float(rec["open"])
        close_px = float(rec["close"])
        if pending is not None and pending != book.side_of(symbol):
            marks = {symbol: open_px}
            for sym, pos in book.positions.items():
                marks.setdefault(sym, pos.entry_price)
            book.set_target(symbol, pending, open_px, bar_date, marks)

        mark_map = {symbol: close_px}
        for sym, pos in book.positions.items():
            mark_map.setdefault(sym, pos.entry_price)
        eq = book.equity(mark_map)
        curve.append(
            {
                "date": bar_date.isoformat(),
                "equity": eq,
                "cash": book.cash,
                "side": book.side_of(symbol),
            }
        )

        mas = {w: ma_maps[w].get(bar_date) for w in windows}
        if i + 1 < len(records):
            pending = target_side(
                rules,
                SignalView(
                    close=close_px,
                    mas=mas,
                    mas_prev=mas_prev,
                    spike=bar_date in spikes,
                    volume=_bar_float(rec.get("volume")),
                    vol_sma={w: vol_maps[w].get(bar_date) for w in reqs["volume_windows"]},
                    rsi={p: rsi_maps[p].get(bar_date) for p in reqs["rsi_periods"]},
                    prior_high={
                        w: high_maps[w].get(bar_date) for w in reqs["donchian_high_windows"]
                    },
                    prior_low={
                        w: low_maps[w].get(bar_date) for w in reqs["donchian_low_windows"]
                    },
                ),
                book.side_of(symbol),
            )
        else:
            pending = None
        mas_prev = mas

    open_pos = book.positions.get(symbol)
    open_payload = None
    if open_pos is not None:
        last_close = float(records[-1]["close"])
        if open_pos.side == "long":
            unreal = (last_close - open_pos.entry_price) * open_pos.shares
        else:
            unreal = (open_pos.entry_price - last_close) * open_pos.shares
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

    return _result(strategy, start, end, costs, capital, book, curve, open_payload)


def _result(
    strategy: dict[str, Any],
    start: date,
    end: date,
    costs: CostModel,
    capital: float,
    book: Portfolio,
    curve: list[dict[str, Any]],
    open_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    equities = [c["equity"] for c in curve] or [capital]
    ending = equities[-1]
    trades = []
    for t in book.trades:
        trades.append(
            {
                **t,
                "entry_price": _round_money(t["entry_price"]),
                "exit_price": _round_money(t["exit_price"]),
                "shares": _round_money(t["shares"], 8),
                "notional": _round_money(t["notional"]),
                "pnl": _round_money(t["pnl"]),
                "return_on_notional": _round_money(t["return_on_notional"], 8),
                "entry_commission": _round_money(t["entry_commission"]),
                "exit_commission": _round_money(t["exit_commission"]),
                "entry_slippage": _round_money(t["entry_slippage"]),
                "exit_slippage": _round_money(t["exit_slippage"]),
            }
        )
    wins = sum(1 for t in book.trades if t["pnl"] > 0)
    n = len(book.trades)
    win_rate = (wins / n) if n else None
    max_dd = _max_drawdown(equities)
    if open_payload:
        open_payload = {
            **open_payload,
            "shares": _round_money(open_payload["shares"], 8),
            "entry_price": _round_money(open_payload["entry_price"]),
            "notional": _round_money(open_payload["notional"]),
            "mark": _round_money(open_payload["mark"]),
            "unrealized_pnl": _round_money(open_payload["unrealized_pnl"]),
        }
    rounded_curve = [
        {
            "date": pt["date"],
            "equity": _round_money(pt["equity"]),
            "cash": _round_money(pt["cash"]),
            "side": pt["side"],
        }
        for pt in curve
    ]
    return {
        "strategy_id": strategy.get("id"),
        "name": strategy.get("name"),
        "symbol": strategy["symbol"],
        "market": strategy["market"],
        "timeframe": strategy["timeframe"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "currency": costs.currency_for(strategy["market"]),
        "starting_capital": _round_money(capital),
        "costs": costs.to_dict(),
        "equity_curve": rounded_curve,
        "trades": trades,
        "open_position": open_payload,
        "metrics": {
            "trade_count": n,
            "win_rate": _round_money(win_rate, 8) if win_rate is not None else None,
            "win_rate_target": WIN_RATE_TARGET,
            "win_rate_meets_target": (win_rate > WIN_RATE_TARGET) if win_rate is not None else None,
            "max_drawdown": _round_money(max_dd, 8),
            "max_drawdown_target": MAX_DD_TARGET,
            "max_drawdown_meets_target": max_dd <= MAX_DD_TARGET,
            "ending_equity": _round_money(ending),
            "total_pnl": _round_money(ending - capital),
            "total_return": _round_money((ending - capital) / capital if capital else None, 8),
        },
    }

"""S1-K12: overnight book for three consecutive down closes. SPY and QQQ only.

`consec_down_n` N=3 is true when each of the last three closes is strictly
below the prior close. The overnight book buys that close and sells the next
stored open. It is not the next-open engine: RSI, relative volume, Donchian,
ROC, EMA, SMA, Williams %R, NR7, and Keltner still fill the next open,
including on SPY/QQQ.

Costs on the golden are US v1.2 defaults. The Top-50 grid stays 25 cells.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.compute_indicators import consec_down_flags, ema
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    CONSEC_DOWN_N_DEFAULT,
    OVERNIGHT_BOOK,
    SignalView,
    condition_true,
    overnight_consec_down_rules,
    signal_requirements,
    validate_rules,
)
from dixon.strategy_board import grid_cells
from tests.test_keltner_k14 import public_tape as keltner_public_tape

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "overnight_consec_down_n3_spy_qqq.json"
GOLDEN_START = date(2024, 1, 2)

# (open, high, low, close). Opens on signal and exit bars differ from those closes.
SPY_BARS = [
    (100.0, 101.0, 99.0, 100.0),
    (100.0, 100.5, 98.5, 99.0),
    (99.0, 99.5, 97.5, 98.0),
    (98.0, 98.5, 96.5, 97.0),  # N=3 signal, buy 97
    (96.25, 97.5, 96.0, 96.5),  # sell 96.25, and another N=3 signal, buy 96.5
    (97.25, 99.0, 97.0, 98.5),  # sell 97.25
    (98.5, 99.0, 97.0, 97.5),
    (97.5, 98.0, 96.0, 96.5),
    (96.5, 97.0, 95.0, 95.5),  # N=3 signal, buy 95.5
    (94.0, 96.0, 93.5, 95.0),  # sell 94, and another N=3 signal, buy 95
    (93.0, 99.0, 92.0, 99.0),  # sell 93
]
QQQ_BARS = [
    (400.0, 401.0, 399.0, 400.0),
    (400.0, 400.5, 397.0, 398.0),
    (398.0, 398.5, 395.0, 396.0),
    (396.0, 396.5, 393.0, 394.0),  # buy 394
    (397.0, 402.0, 396.0, 401.0),  # sell 397
    (401.0, 401.5, 398.0, 399.0),
    (399.0, 399.5, 396.0, 397.0),
    (397.0, 397.5, 393.0, 395.0),  # buy 395
    (392.0, 394.0, 390.0, 396.0),  # sell 392
]

ZERO_COSTS = {
    "hk_commission_bps": 0,
    "hk_spread_ticks": 0,
    "us_commission_per_share": 0,
    "us_commission_min": 0,
    "us_spread_bps": 0,
}


def _day(start: date, offset: int) -> str:
    return (start + timedelta(days=offset)).isoformat()


def _insert(rows, symbol, start=GOLDEN_START, market="US", con=None):
    if con is None:
        con = connect()
    con.execute(
        "DELETE FROM daily_bars WHERE market = ? AND symbol = ?",
        [market, symbol],
    )
    for i, (open_, high, low, close, *rest) in enumerate(rows):
        volume = rest[0] if rest else 1_000_000
        con.execute(
            """
            INSERT INTO daily_bars
              (market, symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [market, symbol, start + timedelta(days=i), open_, high, low, close, volume],
        )
    return con


def _rules(**sides):
    return {
        "long": sides.get("long", {"logic": "AND", "conditions": []}),
        "flat": sides.get("flat", {"logic": "AND", "conditions": []}),
        "short": sides.get("short", {"logic": "AND", "conditions": []}),
        **{k: v for k, v in sides.items() if k not in {"long", "flat", "short"}},
    }


def _public(n=3):
    return overnight_consec_down_rules(n)


def _run(con, rules, start, end, costs=None, symbol="SPY", market="US", timeframe="daily"):
    return run_backtest(
        con,
        {
            "id": "k12",
            "name": "Overnight 3 down",
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "rules": rules,
        },
        start,
        end,
        costs,
    )


def _locked(result):
    return {
        "book": result["book"],
        "fill": result["fill"],
        "currency": result["currency"],
        "starting_capital": result["starting_capital"],
        "metrics": result["metrics"],
        "trades": result["trades"],
        "open_position": result["open_position"],
        "equity_curve": result["equity_curve"],
    }


def _roundtrip(payload):
    return json.loads(json.dumps(payload, sort_keys=True))


def _true_idx(flags: pd.Series) -> list[int]:
    return [i for i, value in enumerate(flags.tolist()) if value == 1.0]


def test_n3_is_three_strict_down_closes_and_a_tie_or_a_wick_does_not_count():
    closes = pd.Series([10.0, 9.0, 8.0, 8.0, 7.0])
    assert _true_idx(consec_down_flags(closes, 1)) == [1, 2, 4]
    assert _true_idx(consec_down_flags(closes, 2)) == [2]
    assert _true_idx(consec_down_flags(closes, 3)) == []
    # A higher high does not cancel a down close. A lower low does not make an up close.
    wicks = pd.Series([10.0, 9.0, 9.5, 8.0])
    assert _true_idx(consec_down_flags(wicks, 1)) == [1, 3]
    assert _true_idx(consec_down_flags(wicks, 3)) == []
    down = pd.Series([10.0, 9.0, 8.0, 7.0])
    assert _true_idx(consec_down_flags(down, 3)) == [3]


def test_public_n3_is_canonical_and_refuses_to_share_the_next_open_path():
    rules = validate_rules(_public())
    assert rules["book"] == OVERNIGHT_BOOK
    assert rules["long"]["conditions"] == [{"type": "consec_down_n", "n": 3}]
    assert rules["flat"]["conditions"] == []
    assert rules["short"]["conditions"] == []
    assert "trail_pct" not in rules
    assert CONSEC_DOWN_N_DEFAULT == 3
    assert signal_requirements(rules)["consec_down"] == [3]

    omitted = validate_rules(_public())
    bare = overnight_consec_down_rules()
    bare["long"]["conditions"][0].pop("n")
    assert validate_rules(bare)["long"]["conditions"][0]["n"] == 3

    legacy = _rules(
        long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        flat={"logic": "AND", "conditions": [{"type": "price_lt_ma", "window": 5}]},
    )
    assert validate_rules(legacy) == legacy
    assert "book" not in validate_rules(legacy)
    assert signal_requirements(validate_rules(legacy))["consec_down"] == []

    view = SignalView(close=1.0, mas={}, mas_prev={}, consec_down={3: 1.0})
    assert condition_true({"type": "consec_down_n", "n": 3}, view)
    assert not condition_true({"type": "consec_down_n", "n": 3}, SignalView(close=1.0, mas={}, mas_prev={}))

    with pytest.raises(ValueError, match="does not fill on the next-open path"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "consec_down_n", "n": 3}]})
        )
    with pytest.raises(ValueError, match="long consec_down_n only"):
        validate_rules(
            {
                "book": "overnight",
                "long": {
                    "logic": "AND",
                    "conditions": [
                        {"type": "consec_down_n", "n": 3},
                        {"type": "rsi_lte", "period": 2, "threshold": 5},
                    ],
                },
                "flat": {"logic": "AND", "conditions": []},
                "short": {"logic": "AND", "conditions": []},
            }
        )
    with pytest.raises(ValueError, match="not a flat or short rule"):
        validate_rules(
            {
                "book": "overnight",
                "long": {"logic": "AND", "conditions": [{"type": "consec_down_n"}]},
                "flat": {"logic": "OR", "conditions": [{"type": "rsi_gte", "period": 2, "threshold": 70}]},
                "short": {"logic": "AND", "conditions": []},
            }
        )
    with pytest.raises(ValueError, match="does not take a trail"):
        validate_rules({**_public(), "trail_pct": 5})
    with pytest.raises(ValueError, match="book must be overnight"):
        validate_rules({**legacy, "book": "next_open"})
    for bad, match in (
        ({"type": "consec_down_n", "n": 0}, "consec_down_n n"),
        ({"type": "consec_down_n", "n": 501}, "consec_down_n n"),
        ({"type": "consec_down_n", "n": True}, "consec_down_n n"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_rules({"book": "overnight", **_rules(long={"logic": "AND", "conditions": [bad]})})
    assert omitted["long"]["conditions"][0]["n"] == 3


def test_overnight_book_buys_the_close_and_sells_the_next_open():
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    try:
        end = start + timedelta(days=len(SPY_BARS) - 1)
        result = _run(con, _public(), start, end, costs=ZERO_COSTS)
    finally:
        con.close()

    assert result["book"] == "overnight"
    assert result["fill"] == "close_to_next_open"
    assert result["open_position"] is None
    trades = result["trades"]
    assert [t["exit_reason"] for t in trades] == ["overnight"] * 4
    assert [(t["entry_date"], t["entry_price"], t["exit_date"], t["exit_price"]) for t in trades] == [
        (_day(start, 3), 97.0, _day(start, 4), 96.25),
        (_day(start, 4), 96.5, _day(start, 5), 97.25),
        (_day(start, 8), 95.5, _day(start, 9), 94.0),
        (_day(start, 9), 95.0, _day(start, 10), 93.0),
    ]
    # Zero costs: the first ticket is 20% of 100_000 bought at the close.
    first = trades[0]
    shares = 20_000 / 97.0
    assert first["shares"] == pytest.approx(shares)
    assert first["pnl"] == pytest.approx((96.25 - 97.0) * shares)
    assert first["entry_price"] != SPY_BARS[3][0]
    assert first["exit_price"] == SPY_BARS[4][0]
    assert first["entry_price"] != SPY_BARS[3][1]


def test_n1_enters_earlier_than_public_n3_and_still_uses_the_close():
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    try:
        end = start + timedelta(days=len(SPY_BARS) - 1)
        early = _run(con, _public(n=1), start, end, costs=ZERO_COSTS)
        public = _run(con, _public(n=3), start, end, costs=ZERO_COSTS)
    finally:
        con.close()
    assert early["trades"][0]["entry_date"] == _day(start, 1)
    assert early["trades"][0]["entry_price"] == 99.0
    assert early["trades"][0]["exit_price"] == SPY_BARS[2][0]
    assert early["trades"][0]["exit_reason"] == "overnight"
    assert public["trades"][0]["entry_date"] == _day(start, 3)
    assert len(early["trades"]) > len(public["trades"])


def test_warmup_bars_complete_the_streak_and_the_last_bar_does_not_enter():
    """The third down close can be the first in-range bar. No next open, no entry."""
    start = date(2024, 3, 1)
    rows = [
        (10, 10, 9, 10),
        (10, 10, 8, 9),
        (9, 9, 7, 8),
        (8, 8, 6, 7),  # completes N=3 using the three closes above
        (6, 7, 5, 6),
    ]
    con = _insert(rows, "QQQ", start=start)
    try:
        window_start = start + timedelta(days=3)
        held = _run(con, _public(), window_start, start + timedelta(days=4), symbol="QQQ")
        clipped = _run(con, _public(), window_start, window_start, symbol="QQQ")
    finally:
        con.close()
    assert len(held["trades"]) == 1
    assert held["trades"][0]["entry_date"] == window_start.isoformat()
    assert held["trades"][0]["entry_price"] == 7.0
    assert held["trades"][0]["exit_date"] == (start + timedelta(days=4)).isoformat()
    assert held["trades"][0]["exit_price"] == 6.0
    assert held["open_position"] is None
    assert clipped["trades"] == []
    assert clipped["open_position"] is None
    assert clipped["book"] == "overnight"


def test_universe_is_spy_and_qqq_only():
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    _insert(QQQ_BARS, "QQQ", start=start, con=con)
    _insert(SPY_BARS, "AAPL", start=start, con=con)
    _insert([(100, 100, 100, 100)], "0700.HK", market="HK", start=start, con=con)
    try:
        end = start + timedelta(days=len(SPY_BARS) - 1)
        spy = _run(con, _public(), start, end, symbol="spy")
        qqq = _run(con, _public(), start, start + timedelta(days=len(QQQ_BARS) - 1), symbol="QQQ")
        assert spy["symbol"] == "SPY"
        assert qqq["symbol"] == "QQQ"
        assert spy["trades"][0]["entry_price"] == 97.0
        assert qqq["trades"][0]["entry_price"] == 394.0
        for symbol, market in (("AAPL", "US"), ("MSFT", "US"), ("IWM", "US"), ("0700.HK", "HK")):
            with pytest.raises(ValueError, match="SPY and QQQ only"):
                _run(con, _public(), start, end, symbol=symbol, market=market)
        with pytest.raises(ValueError, match="SPY and QQQ only"):
            _run(con, _public(), start, end, symbol="SPY", market="HK")
        with pytest.raises(ValueError, match="SPY and QQQ only"):
            _run(con, _public(), start, end, symbol="SPY", timeframe="weekly")
    finally:
        con.close()


def _assert_next_open(result, rows, start):
    by_date = {_day(start, i): row for i, row in enumerate(rows)}
    fills = list(result["trades"])
    if result["open_position"] is not None:
        fills.append(result["open_position"])
    assert fills, "next-open strategy produced no fill"
    assert "book" not in result
    assert "fill" not in result
    for fill in result["trades"]:
        assert fill["exit_reason"] in {"signal", "trail"}
        assert fill["entry_price"] == by_date[fill["entry_date"]][0]
        assert fill["exit_price"] == by_date[fill["exit_date"]][0]
        assert by_date[fill["entry_date"]][0] != by_date[fill["entry_date"]][3]
    if result["open_position"] is not None:
        opened = result["open_position"]
        assert opened["entry_price"] == by_date[opened["entry_date"]][0]
        assert by_date[opened["entry_date"]][0] != by_date[opened["entry_date"]][3]


def test_willr_nr7_and_keltner_still_fill_the_next_open():
    """K13/K11/K14 stay on the next-open book. The overnight fill does not attach."""
    start = date(2024, 6, 1)
    will_rows = [
        (11, 12, 8, 11),
        (9, 12, 8, 8),  # %R(2) = -100, long
        (16, 17, 14, 15),  # next-open entry
        (19, 20, 10, 20),  # %R = 0, flat
        (7, 8, 6, 7),
    ]
    con = _insert(will_rows, "SPY", start=start)
    try:
        end = start + timedelta(days=len(will_rows) - 1)
        will = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 2, "threshold": -95}]},
                flat={"logic": "OR", "conditions": [{"type": "willr_gte", "period": 2, "threshold": -5}]},
            ),
            start,
            end,
            symbol="SPY",
        )
        _assert_next_open(will, will_rows, start)
        assert will["trades"][0]["entry_price"] == 16
        assert will["trades"][0]["exit_price"] == 7
        assert will["trades"][0]["exit_reason"] == "signal"
    finally:
        con.close()

    nr_rows = [
        (10, 16, 10, 12),
        (11, 12, 11, 11.5),  # high-low is the narrower of the two
        (20.5, 21, 19, 20),
        (9, 22, 8, 21),  # close above the prior high
        (6, 7, 5, 6),
    ]
    con = _insert(nr_rows, "QQQ", start=start)
    try:
        end = start + timedelta(days=len(nr_rows) - 1)
        nr = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 2}]},
                flat={"logic": "OR", "conditions": [{"type": "donchian_high", "window": 1}]},
            ),
            start,
            end,
            symbol="QQQ",
        )
        _assert_next_open(nr, nr_rows, start)
        assert nr["trades"][0]["entry_price"] == 20.5
        assert nr["trades"][0]["exit_price"] == 9
        assert nr["trades"][0]["exit_reason"] == "signal"
    finally:
        con.close()

    k_rows = keltner_public_tape()
    con = _insert(k_rows, "AAPL", start=start)
    try:
        end = start + timedelta(days=len(k_rows) - 1)
        kelt = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [{"type": "keltner_below", "keltner_len": 6, "atr_mult": 1.3}],
                },
                flat={
                    "logic": "OR",
                    "conditions": [{"type": "keltner_above", "keltner_len": 6, "atr_mult": 1.3}],
                },
            ),
            start,
            end,
            symbol="AAPL",
        )
        _assert_next_open(kelt, k_rows, start)
        assert kelt["trades"][0]["entry_price"] == pytest.approx(98.80)
        assert kelt["trades"][0]["exit_price"] == pytest.approx(100.40)
        assert kelt["trades"][0]["exit_reason"] == "signal"
        assert "book" not in kelt
    finally:
        con.close()


def test_next_open_families_still_fill_the_next_open_on_spy_and_qqq():
    """Overnight fills must not attach to the existing next-open book.

    The same process runs those strategies, including on SPY and QQQ, and
    the overnight book on the same SPY tape. Entry prices disagree.
    Williams %R, NR7, and Keltner are covered in the sibling test; they stay
    on this next-open path.
    """
    start = date(2024, 4, 1)

    sma_rows = [
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (11.0, 13, 10, 12),  # close > SMA(5); fill is the next open
        (15.0, 16, 14, 12),
        (13.0, 14, 7, 8),  # close < SMA(5)
        (9.0, 10, 8, 8),
    ]
    con = _insert(sma_rows, "SPY", start=start)
    try:
        end = start + timedelta(days=len(sma_rows) - 1)
        sma = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
                flat={"logic": "AND", "conditions": [{"type": "price_lt_ma", "window": 5}]},
            ),
            start,
            end,
            symbol="SPY",
        )
        _assert_next_open(sma, sma_rows, start)
        assert sma["trades"][0]["entry_price"] == 15.0
        assert sma["trades"][0]["exit_price"] == 9.0
        assert sma["trades"][0]["entry_date"] == _day(start, 6)
        assert sma["trades"][0]["entry_price"] != 12.0
    finally:
        con.close()

    don_rows = [
        (10, 10, 10, 10),
        (11, 11, 9, 12),  # close > prior high
        (20.5, 21, 19, 20),
        (8, 8, 7, 7),  # close < prior low
        (5, 5, 4, 5),
    ]
    con = _insert(don_rows, "QQQ", start=start)
    try:
        end = start + timedelta(days=len(don_rows) - 1)
        don = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "donchian_high", "window": 1}]},
                flat={"logic": "OR", "conditions": [{"type": "donchian_low", "window": 1}]},
            ),
            start,
            end,
            symbol="QQQ",
        )
        _assert_next_open(don, don_rows, start)
        assert don["trades"][0]["entry_price"] == 20.5
        assert don["trades"][0]["exit_price"] == 5.0
        assert don["trades"][0]["entry_date"] == _day(start, 2)
    finally:
        con.close()

    rvol_rows = [
        (10, 10, 10, 10, 100),
        (10, 10, 10, 10, 100),
        (10, 10, 10, 10, 1000),
        (41, 42, 39, 40, 100),
    ]
    con = _insert(rvol_rows, "MSFT", start=start)
    try:
        end = start + timedelta(days=len(rvol_rows) - 1)
        rvol = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "rel_volume", "k": 1.5, "window": 3}]}
            ),
            start,
            end,
            symbol="MSFT",
        )
        _assert_next_open(rvol, rvol_rows, start)
        assert rvol["open_position"]["entry_price"] == 41.0
        assert rvol["open_position"]["entry_date"] == _day(start, 3)
        assert rvol["trades"] == []
    finally:
        con.close()

    rsi_closes = [round(20 + 0.4 * i, 10) for i in range(13)]
    price = rsi_closes[-1]
    for step in (-0.3, -0.3, -0.3, -0.2, -0.25, 0.6, 1.2, 1.8, 2.0):
        price = round(price + step, 10)
        rsi_closes.append(price)
    rsi_rows = [(c + 5.0, c + 6.0, c - 1.0, c, 1_000_000) for c in rsi_closes]
    con = _insert(rsi_rows, "AAPL", start=start)
    try:
        end = start + timedelta(days=len(rsi_rows) - 1)
        rsi = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "rsi_lte", "period": 2, "threshold": 5}]},
                flat={"logic": "OR", "conditions": [{"type": "rsi_gte", "period": 2, "threshold": 70}]},
            ),
            start,
            end,
            symbol="AAPL",
        )
        _assert_next_open(rsi, rsi_rows, start)
        assert rsi["trades"][0]["entry_date"] == _day(start, 18)
        assert rsi["trades"][0]["entry_price"] == pytest.approx(rsi_closes[18] + 5.0)
        assert rsi["trades"][0]["entry_price"] != pytest.approx(rsi_closes[17])
        assert rsi["trades"][0]["exit_reason"] == "signal"
    finally:
        con.close()

    roc_rows = [
        (103, 104, 99, 100),
        (105, 106, 101, 102),  # ROC(1) = +2
        (106, 107, 100, 103),
        (90, 91, 80, 88),  # ROC(1) <= -1, reverse
        (84, 86, 83, 85),
    ]
    con = _insert(roc_rows, "IWM", start=start)
    try:
        end = start + timedelta(days=len(roc_rows) - 1)
        roc = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "roc_gte", "period": 1, "threshold": 1}]},
                short={"logic": "AND", "conditions": [{"type": "roc_lte", "period": 1, "threshold": -1}]},
            ),
            start,
            end,
            symbol="IWM",
        )
        _assert_next_open(roc, roc_rows, start)
        assert roc["trades"][0]["entry_price"] == 106
        assert roc["trades"][0]["exit_price"] == 84
        assert roc["trades"][0]["side"] == "long"
    finally:
        con.close()

    trail_rows = [
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (10.5, 11, 9, 10),
        (12.5, 13, 11, 12),
        (13.0, 20, 12, 19),
        (17.0, 18, 16, 18),
    ]
    con = _insert(trail_rows, "AAPL", start=start)
    try:
        end = start + timedelta(days=len(trail_rows) - 1)
        trail_rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]}
        )
        trail_rules["trail_pct"] = 5
        trailed = _run(con, trail_rules, start, end, symbol="AAPL")
        _assert_next_open(trailed, trail_rows, start)
        assert trailed["trades"][0]["exit_reason"] == "trail"
        assert trailed["trades"][0]["entry_price"] == 13.0
        assert trailed["trades"][0]["exit_price"] == 17.0
    finally:
        con.close()

    ema_closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
    ema_rows = [(c + 1.0, c + 1.5, c - 0.5, c, 1000) for c in ema_closes]
    fast = ema(pd.Series(ema_closes), 2)
    slow = ema(pd.Series(ema_closes), 3)
    signal_at = next(i for i, (f, s) in enumerate(zip(fast, slow)) if pd.notna(f) and pd.notna(s) and f > s)
    con = _insert(ema_rows, "QQQ", start=start)
    try:
        end = start + timedelta(days=len(ema_rows) - 1)
        stacked = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [{"type": "ema_gt_ema", "fast": 2, "slow": 3}],
                }
            ),
            start,
            end,
            symbol="QQQ",
        )
        _assert_next_open(stacked, ema_rows, start)
        assert stacked["open_position"]["entry_date"] == _day(start, signal_at + 1)
        assert stacked["open_position"]["entry_price"] == ema_closes[signal_at + 1] + 1.0
    finally:
        con.close()

    con = _insert(SPY_BARS, "SPY", start=start)
    try:
        end = start + timedelta(days=len(SPY_BARS) - 1)
        overnight = _run(con, _public(), start, end, costs=ZERO_COSTS, symbol="SPY")
        assert overnight["book"] == "overnight"
        assert overnight["trades"][0]["entry_price"] == 97.0
        assert overnight["trades"][0]["entry_date"] == _day(start, 3)
        assert overnight["trades"][0]["exit_price"] == 96.25
        assert overnight["trades"][0]["exit_reason"] == "overnight"
        assert overnight["trades"][0]["entry_price"] != sma["trades"][0]["entry_price"]
    finally:
        con.close()


def test_golden_spy_and_qqq_closed_pnl_is_bit_stable_on_us_v1_2():
    """Default US costs. The fixture is the closed public N=3 book for both names."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    _insert(QQQ_BARS, "QQQ", start=start, con=con)
    try:
        spy = _run(con, _public(), start, start + timedelta(days=len(SPY_BARS) - 1))
        qqq = _run(con, _public(), start, start + timedelta(days=len(QQQ_BARS) - 1), symbol="QQQ")
    finally:
        con.close()

    for result, bars in ((spy, SPY_BARS), (qqq, QQQ_BARS)):
        assert result["costs"] == CostModel().to_dict()
        assert result["currency"] == "USD"
        assert result["starting_capital"] == 100_000
        assert result["book"] == "overnight"
        assert result["fill"] == "close_to_next_open"
        assert result["open_position"] is None
        assert result["metrics"]["trade_count"] == len(result["trades"])
        assert result["metrics"]["trade_count"] > 0
        assert all(t["exit_reason"] == "overnight" for t in result["trades"])
        assert all(t["side"] == "long" for t in result["trades"])
        assert result["metrics"]["ending_equity"] == pytest.approx(
            result["starting_capital"] + result["metrics"]["total_pnl"]
        )
        assert result["equity_curve"][-1]["side"] == "flat"

    assert spy["metrics"]["total_pnl"] != qqq["metrics"]["total_pnl"]
    assert _roundtrip({"spy": _locked(spy), "qqq": _locked(qqq)}) == {
        "spy": expected["spy"],
        "qqq": expected["qqq"],
    }
    assert expected["n"] == 3
    assert expected["book"] == "overnight"
    assert expected["fill"] == "close_to_next_open"
    assert expected["costs"] == "us-v1.2-defaults"
    assert expected["symbols"] == ["SPY", "QQQ"]
    assert expected["spy_total_pnl"] == spy["metrics"]["total_pnl"]
    assert expected["qqq_total_pnl"] == qqq["metrics"]["total_pnl"]
    assert expected["spy_trade_count"] == 4
    assert expected["qqq_trade_count"] == 2


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/overnight_consec_down_n3_spy_qqq.json. Not used by pytest."""
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    _insert(QQQ_BARS, "QQQ", start=start, con=con)
    try:
        spy = _run(con, _public(), start, start + timedelta(days=len(SPY_BARS) - 1))
        qqq = _run(con, _public(), start, start + timedelta(days=len(QQQ_BARS) - 1), symbol="QQQ")
    finally:
        con.close()
    payload = {
        "symbols": ["SPY", "QQQ"],
        "n": 3,
        "book": "overnight",
        "fill": "close_to_next_open",
        "costs": "us-v1.2-defaults",
        "spy_total_pnl": spy["metrics"]["total_pnl"],
        "qqq_total_pnl": qqq["metrics"]["total_pnl"],
        "spy_trade_count": spy["metrics"]["trade_count"],
        "qqq_trade_count": qqq["metrics"]["trade_count"],
        "spy": _roundtrip(_locked(spy)),
        "qqq": _roundtrip(_locked(qqq)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_board_stays_25_cells_and_does_not_score_consec_down():
    cells = grid_cells()
    assert len(cells) == 25
    families = {cell.family for cell in cells}
    assert families == {"rsi", "rvol", "donchian", "anchor"}
    assert sum(1 for cell in cells if cell.family == "anchor") == 5
    for cell in cells:
        rules = cell.rules()
        assert "book" not in rules
        assert "consec_down_n" not in cell.rules_json
        for side in ("long", "flat", "short"):
            for cond in rules[side]["conditions"]:
                assert cond["type"] != "consec_down_n"


def test_api_persists_the_public_book_and_rejects_other_symbols():
    start = GOLDEN_START
    con = _insert(SPY_BARS, "SPY", start=start)
    _insert(QQQ_BARS, "QQQ", start=start, con=con)
    con.close()
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={"name": "Overnight 3 down", "symbol": "SPY", "timeframe": "daily", "rules": _public()},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["book"] == "overnight"
    assert body["rules"]["long"]["conditions"] == [{"type": "consec_down_n", "n": 3}]
    assert "trail_pct" not in body["rules"]

    run = client.post(
        f"/strategies/{body['id']}/backtest",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(days=len(SPY_BARS) - 1)).isoformat(),
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["book"] == "overnight"
    assert run.json()["fill"] == "close_to_next_open"
    assert run.json()["trades"][0]["entry_price"] == 97.0
    assert run.json()["trades"][0]["exit_price"] == 96.25
    assert run.json()["costs"]["us_commission_per_share"] == 0.005
    assert run.json()["costs"]["us_spread_bps"] == 5

    rejected = client.post(
        "/strategies",
        json={"name": "AAPL overnight", "symbol": "AAPL", "rules": _public()},
    )
    assert rejected.status_code == 400
    assert "SPY and QQQ" in rejected.text

    naked = client.post(
        "/strategies",
        json={
            "name": "naked down",
            "symbol": "SPY",
            "rules": _rules(long={"logic": "AND", "conditions": [{"type": "consec_down_n", "n": 3}]}),
        },
    )
    assert naked.status_code == 400
    assert "next-open" in naked.text

    weekly = client.post(
        "/strategies",
        json={
            "name": "weekly overnight",
            "symbol": "QQQ",
            "timeframe": "weekly",
            "rules": _public(),
        },
    )
    assert weekly.status_code == 400


def test_docs_and_the_research_panel_label_the_overnight_spy_qqq_preset():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    formulas = (ROOT / "indicators" / "FORMULAS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    prd = (ROOT / "PRD.md").read_text()
    assert 'id: "consecDown"' in script
    assert "Overnight 3 down (SPY/QQQ)" in script
    assert 'book: "overnight"' in script
    assert '{ type: "consec_down_n", n: 3 }' in script
    assert "consec_down_n: { n: 3 }" in script
    assert "Close down N days" in script
    assert "not a next-open entry" in script
    assert "Top-50 board" in script
    assert "Buy that third close" in script
    assert "next session's open" in script
    assert "function isConsecDownType" in script
    assert 'pattern.book === "overnight"' in script
    assert "Overnight 3 down is SPY and QQQ only" in html
    assert "Buy that third close and sell the next open" in html
    assert "consec_down_n" in formulas
    assert "SPY" in formulas and "QQQ" in formulas
    assert "s1-board-v1" in formulas
    assert "buy the signal close, sell the next stored bar's open" in readme
    assert "stays 25 cells" in readme
    assert "Overnight 3 down" in prd
    assert "s1-board-v1" in prd

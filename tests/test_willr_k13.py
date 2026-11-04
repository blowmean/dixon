"""S1-K13: Williams %R threshold predicates.

%R = (highest high − close) / (highest high − lowest low) × −100 over the
last N bars, including today. `willr_lte` is true at or below a threshold
(default −80, oversold). `willr_gte` is true at or above one (default −20,
overbought). A zero high-low range is missing, so the condition is false.

The public pattern is long on %R(14) ≤ −80 and flat on %R(14) ≥ −20. The
band between those lines holds the open side. Fills are the next stored
open. There is no trail. This is not Donchian (that drops today) and not
stochastic %K (on a positive range, %R = %K − 100).

Signal cases use zero costs. The golden reruns the same AAPL tape with the
default US book. The overnight s1-board-v1 grid does not grow a cell.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.compute_indicators import prior_high, prior_low, williams_r
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    WILLR_GTE_THRESHOLD_DEFAULT,
    WILLR_LTE_THRESHOLD_DEFAULT,
    WILLR_PERIOD_DEFAULT,
    SignalView,
    condition_true,
    signal_requirements,
    target_side,
    validate_rules,
    windows_used,
)
from dixon.strategy_board import GRID_VERSION, grid_cells

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "willr_n14_aapl.json"
GOLDEN_START = date(2024, 1, 2)

ZERO_COSTS = {
    "hk_commission_bps": 0,
    "hk_spread_ticks": 0,
    "us_commission_per_share": 0,
    "us_commission_min": 0,
    "us_spread_bps": 0,
}


def _empty_sides():
    return {
        "long": {"logic": "AND", "conditions": []},
        "flat": {"logic": "AND", "conditions": []},
        "short": {"logic": "AND", "conditions": []},
    }


def _rules(**sides):
    rules = _empty_sides()
    rules.update(sides)
    return rules


def _willr(period=14, long_at=-80, exit_at=-20):
    """Public pattern. Oversold longs, overbought flats. No trail."""
    return _rules(
        long={
            "logic": "AND",
            "conditions": [{"type": "willr_lte", "period": period, "threshold": long_at}],
        },
        flat={
            "logic": "OR",
            "conditions": [{"type": "willr_gte", "period": period, "threshold": exit_at}],
        },
    )


def public_rows():
    """Thirty AAPL bars for the %R(14) public book.

    Bars 0–13 sit in the middle of a 90–110 range. %R is −50 once the
    window is full, so nothing fires. Bar 14 closes at 92 (−90) and the
    long fills at bar 15's open, 93, not at 92. Bars 15–17 stay at −50, so
    the long is held. Bar 18 closes at 107 (−15) and the exit fills at bar
    19's open, 106, not at 107. The old 110/90 range then rolls out of the
    window. Bar 28 makes a new low (−96) and the second long fills at bar
    29's open, 82, and stays open because that bar is the last one.
    """
    rows: list[tuple[float, float, float, float, int]] = []
    for _ in range(14):
        rows.append((100.0, 110.0, 90.0, 100.0, 1_000_000))
    rows.append((100.0, 100.0, 90.0, 92.0, 1_000_000))
    rows.append((93.0, 102.0, 93.0, 100.0, 1_000_000))
    rows.append((100.0, 104.0, 96.0, 100.0, 1_000_000))
    rows.append((100.0, 103.0, 97.0, 100.0, 1_000_000))
    rows.append((100.0, 108.0, 100.0, 107.0, 1_000_000))
    rows.append((106.0, 106.0, 99.0, 100.0, 1_000_000))
    for _ in range(8):
        rows.append((100.0, 104.0, 96.0, 100.0, 1_000_000))
    rows.append((100.0, 100.0, 80.0, 81.0, 1_000_000))
    rows.append((82.0, 100.0, 82.0, 96.0, 1_000_000))
    assert len(rows) == 30
    return rows


def _insert(rows, symbol="AAPL", start=GOLDEN_START, market="US"):
    con = connect()
    for i, (open_, high, low, close, volume) in enumerate(rows):
        con.execute(
            """
            INSERT INTO daily_bars
              (market, symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [market, symbol, start + timedelta(days=i), open_, high, low, close, volume],
        )
    return con


def _run(con, rules, start, end, costs=None, symbol="AAPL", market="US"):
    return run_backtest(
        con,
        {
            "id": "k13",
            "name": "williams r",
            "symbol": symbol,
            "market": market,
            "timeframe": "daily",
            "rules": rules,
        },
        start,
        end,
        ZERO_COSTS if costs is None else costs,
    )


def _day(start, offset):
    return (start + timedelta(days=offset)).isoformat()


def _spine(result):
    closed = [
        (
            t["side"],
            t["entry_date"],
            t["entry_price"],
            t["exit_date"],
            t["exit_price"],
            t["exit_reason"],
        )
        for t in result["trades"]
    ]
    op = result["open_position"]
    opened = None if op is None else (op["side"], op["entry_date"], op["entry_price"])
    return closed, opened


def _locked(result):
    return {
        "currency": result["currency"],
        "starting_capital": result["starting_capital"],
        "metrics": result["metrics"],
        "trades": result["trades"],
        "open_position": result["open_position"],
        "equity_curve": result["equity_curve"],
    }


def _roundtrip(payload):
    return json.loads(json.dumps(payload, sort_keys=True))


def _oracle(high, low, close, period):
    """Independent window: max high and min low of the last `period` bars, including i."""
    out = []
    h = list(high)
    lo = list(low)
    c = list(close)
    for i in range(len(c)):
        if i < period - 1:
            out.append(np.nan)
            continue
        window_h = h[i - period + 1 : i + 1]
        window_l = lo[i - period + 1 : i + 1]
        if any(pd.isna(v) for v in window_h) or any(pd.isna(v) for v in window_l) or pd.isna(c[i]):
            out.append(np.nan)
            continue
        hh = max(window_h)
        ll = min(window_l)
        if hh <= ll:
            out.append(np.nan)
        else:
            out.append((hh - c[i]) / (hh - ll) * -100.0)
    return out


def _same(got: pd.Series, expect) -> None:
    assert len(got) == len(expect)
    for i, (left, right) in enumerate(zip(got.tolist(), expect)):
        if pd.isna(right):
            assert pd.isna(left), i
        else:
            assert left == right, (i, left, right)


def test_formula_matches_an_independent_window_and_the_stochastic_identity():
    rows = public_rows()
    high = pd.Series([r[1] for r in rows], dtype=float)
    low = pd.Series([r[2] for r in rows], dtype=float)
    close = pd.Series([r[3] for r in rows], dtype=float)
    got = williams_r(high, low, close, 14)
    _same(got, _oracle(high, low, close, 14))
    # First thirteen bars are warm-up. Bar 13 is the middle of the range.
    assert all(pd.isna(v) for v in got.iloc[:13])
    assert float(got.iloc[13]) == -50.0
    assert float(got.iloc[14]) == -90.0
    assert float(got.iloc[18]) == -15.0

    hh = high.rolling(14, min_periods=14).max()
    ll = low.rolling(14, min_periods=14).min()
    percent_k = (close - ll) / (hh - ll) * 100.0
    identity = percent_k - 100.0
    finite = got.notna()
    assert finite.any()
    pd.testing.assert_series_equal(got[finite], identity[finite], check_names=False)

    # Same closes, different highs: %R moves. A close-only study would not.
    same_close = pd.Series([9.0, 9.0, 9.0])
    tight = williams_r(pd.Series([10.0, 10.0, 10.0]), pd.Series([8.0, 8.0, 8.0]), same_close, 3)
    wide = williams_r(pd.Series([20.0, 20.0, 20.0]), pd.Series([8.0, 8.0, 8.0]), same_close, 3)
    assert float(tight.iloc[2]) == -50.0
    assert float(wide.iloc[2]) != float(tight.iloc[2])
    assert float(wide.iloc[2]) < -80.0


def test_today_is_inside_the_window_and_a_flat_range_is_missing():
    """A close on today's high is 0. Donchian still sees the prior high."""
    high = pd.Series([10.0, 10.0, 12.0])
    low = pd.Series([8.0, 8.0, 11.0])
    close = pd.Series([9.0, 9.0, 12.0])
    got = williams_r(high, low, close, 3)
    assert pd.isna(got.iloc[0]) and pd.isna(got.iloc[1])
    assert float(got.iloc[2]) == 0.0
    # Dropping today would leave the prior high at 10, and the close is above it.
    assert float(prior_high(high, 2).iloc[2]) == 10.0

    at_low_h = pd.Series([10.0, 10.0, 10.0])
    at_low_l = pd.Series([8.0, 8.0, 6.0])
    at_low_c = pd.Series([9.0, 9.0, 6.0])
    at_low = williams_r(at_low_h, at_low_l, at_low_c, 3)
    assert float(at_low.iloc[2]) == -100.0
    # The prior low is 8. Including today's 6 is what prints −100, not a prior-only ratio.
    assert float(prior_low(at_low_l, 2).iloc[2]) == 8.0

    flat = williams_r(pd.Series([5.0, 5.0, 5.0]), pd.Series([5.0, 5.0, 5.0]), pd.Series([5.0, 5.0, 5.0]), 3)
    assert all(pd.isna(v) for v in flat.tolist())

    # A missing high inside the window leaves %R missing. The oracle agrees.
    gappy_h = pd.Series([10.0, np.nan, 12.0, 11.0])
    gappy_l = pd.Series([8.0, 7.0, 9.0, 8.0])
    gappy_c = pd.Series([9.0, 8.0, 11.0, 10.0])
    gappy = williams_r(gappy_h, gappy_l, gappy_c, 3)
    _same(gappy, _oracle(gappy_h, gappy_l, gappy_c, 3))
    assert all(pd.isna(v) for v in gappy.tolist())


def test_hand_period_3_lands_on_the_threshold_lines():
    """(10 − 6) / (10 − 5) × −100 is −80. The next window's close on the way up is −20."""
    high = pd.Series([10.0, 9.0, 8.0, 10.0])
    low = pd.Series([8.0, 7.0, 5.0, 9.0])
    close = pd.Series([9.0, 8.0, 6.0, 9.0])
    got = williams_r(high, low, close, 3)
    assert float(got.iloc[2]) == -80.0
    assert float(got.iloc[3]) == -20.0
    assert (4.0 / 5.0) * -100.0 == -80.0


def test_bounds_are_inclusive_and_a_missing_value_is_false():
    lte = {"type": "willr_lte", "period": 14, "threshold": -80.0}
    gte = {"type": "willr_gte", "period": 14, "threshold": -20.0}
    assert condition_true(lte, SignalView(close=1, mas={}, mas_prev={}, willr={14: -80.0}))
    assert condition_true(lte, SignalView(close=1, mas={}, mas_prev={}, willr={14: -100.0}))
    assert not condition_true(lte, SignalView(close=1, mas={}, mas_prev={}, willr={14: -79.9}))
    assert condition_true(gte, SignalView(close=1, mas={}, mas_prev={}, willr={14: -20.0}))
    assert condition_true(gte, SignalView(close=1, mas={}, mas_prev={}, willr={14: 0.0}))
    assert not condition_true(gte, SignalView(close=1, mas={}, mas_prev={}, willr={14: -20.1}))
    assert not condition_true(lte, SignalView(close=1, mas={}, mas_prev={}, willr={14: None}))
    assert not condition_true(gte, SignalView(close=1, mas={}, mas_prev={}))


def test_omitted_knobs_default_and_do_not_rewrite_old_rules():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 20}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 20}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert "trail_pct" not in validate_rules(legacy)
    assert signal_requirements(validate_rules(legacy))["willr_periods"] == []

    rules = validate_rules(_willr())
    assert rules["long"]["conditions"] == [
        {"type": "willr_lte", "period": 14, "threshold": -80.0}
    ]
    assert rules["flat"]["logic"] == "OR"
    assert rules["flat"]["conditions"] == [
        {"type": "willr_gte", "period": 14, "threshold": -20.0}
    ]
    assert rules["short"]["conditions"] == []
    assert "trail_pct" not in rules
    assert windows_used(rules) == []
    assert signal_requirements(rules)["willr_periods"] == [14]
    assert WILLR_PERIOD_DEFAULT == 14
    assert WILLR_LTE_THRESHOLD_DEFAULT == -80.0
    assert WILLR_GTE_THRESHOLD_DEFAULT == -20.0

    omitted = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "willr_lte"}]},
            flat={"logic": "OR", "conditions": [{"type": "willr_gte"}]},
        )
    )
    assert omitted["long"]["conditions"] == rules["long"]["conditions"]
    assert omitted["flat"]["conditions"] == rules["flat"]["conditions"]

    # 14.0 is an integer period. The threshold stays on the oscillator.
    coerced = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [{"type": "willr_lte", "period": 14.0, "threshold": -80}],
            }
        )
    )
    assert coerced["long"]["conditions"] == [
        {"type": "willr_lte", "period": 14, "threshold": -80.0}
    ]
    ends = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": -100}]},
            flat={"logic": "OR", "conditions": [{"type": "willr_gte", "threshold": 0}]},
        )
    )
    assert ends["long"]["conditions"][0]["threshold"] == -100.0
    assert ends["flat"]["conditions"][0]["threshold"] == 0.0

    mixed = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 14}]},
            short={"logic": "AND", "conditions": [{"type": "willr_gte", "period": 5}]},
        )
    )
    assert signal_requirements(mixed)["willr_periods"] == [5, 14]


def test_rejects_a_bad_period_or_threshold():
    with pytest.raises(ValueError, match="willr_lte period"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 0}]}))
    with pytest.raises(ValueError, match="willr_gte period"):
        validate_rules(_rules(flat={"logic": "OR", "conditions": [{"type": "willr_gte", "period": 501}]}))
    with pytest.raises(ValueError, match="willr_lte period"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": True}]}))
    with pytest.raises(ValueError, match="willr_lte period"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 14.5}]}))
    with pytest.raises(ValueError, match="willr_lte threshold"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": -100.1}]})
        )
    with pytest.raises(ValueError, match="willr_gte threshold"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "willr_gte", "threshold": 0.1}]})
        )
    with pytest.raises(ValueError, match="willr_lte threshold"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": "-80"}]})
        )


def test_public_book_enters_and_exits_at_the_next_open_and_holds_the_middle():
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _willr(), start, end)
    finally:
        con.close()

    assert result["metrics"]["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["side"] == "long"
    assert trade["entry_date"] == _day(start, 15)
    assert trade["entry_price"] == 93.0
    assert trade["entry_price"] != rows[14][3]
    assert trade["exit_date"] == _day(start, 19)
    assert trade["exit_price"] == 106.0
    assert trade["exit_price"] != rows[18][3]
    assert trade["exit_reason"] == "signal"
    # Bars 15–17 print −50. The exit is bar 19, so those sessions were held.
    assert trade["exit_date"] != _day(start, 16)
    opened = result["open_position"]
    assert opened["side"] == "long"
    assert opened["entry_date"] == _day(start, 29)
    assert opened["entry_price"] == 82.0
    assert opened["entry_price"] != rows[28][3]


def test_bars_before_the_window_warm_the_lookback():
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        late = _run(con, _willr(), start + timedelta(days=14), end)
    finally:
        con.close()
    # Bar 14 is the first in-range close and already ≤ −80 because bars 0–13 exist.
    assert late["trades"][0]["entry_date"] == _day(start, 15)

    # The same sixteen bars, stored as their own symbol, have no history before them.
    tail = rows[14:]
    con = _insert(tail, symbol="MSFT", start=start)
    try:
        cold = _run(con, _willr(), start, start + timedelta(days=len(tail) - 1), symbol="MSFT")
    finally:
        con.close()
    assert cold["trades"] == []
    assert cold["open_position"]["side"] == "long"
    assert cold["open_position"]["entry_date"] == _day(start, len(tail) - 1)


def test_a_flat_tape_does_not_trade_and_period_changes_the_book():
    start = date(2024, 3, 1)
    flat_rows = [(100.0, 100.0, 100.0, 100.0, 1_000_000) for _ in range(20)]
    con = _insert(flat_rows, start=start)
    try:
        end = start + timedelta(days=len(flat_rows) - 1)
        flat = _run(con, _willr(), start, end)
    finally:
        con.close()
    assert flat["trades"] == []
    assert flat["open_position"] is None

    rows = public_rows()
    con = _insert(rows, symbol="MSFT", start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        slow = _run(con, _willr(period=14), start, end, symbol="MSFT")
        fast = _run(con, _willr(period=2), start, end, symbol="MSFT")
    finally:
        con.close()
    assert _spine(slow) != _spine(fast)
    assert slow["trades"][0]["entry_date"] == _day(start, 15)


def test_long_beats_a_flat_rule_and_the_predicate_can_sit_on_short():
    """A still-true long is not flattened. `willr_gte` on short is a different book."""
    start = date(2024, 4, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        held = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": 0}]},
                flat={"logic": "OR", "conditions": [{"type": "willr_gte", "threshold": -100}]},
            ),
            start,
            end,
        )
        shorted = _run(
            con,
            _rules(short={"logic": "AND", "conditions": [{"type": "willr_gte"}]}),
            start,
            end,
        )
    finally:
        con.close()
    assert held["trades"] == []
    assert held["open_position"]["side"] == "long"
    assert held["open_position"]["entry_date"] == _day(start, 14)
    assert held["open_position"]["entry_price"] == 100.0
    assert shorted["trades"] == []
    assert shorted["open_position"]["side"] == "short"
    assert shorted["open_position"]["entry_date"] == _day(start, 19)
    assert shorted["open_position"]["entry_price"] == 106.0


def test_willr_ands_with_rel_volume_and_an_ma_rule():
    start = date(2024, 5, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        alone = _run(con, _willr(), start, end)
        quiet = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "willr_lte", "period": 14, "threshold": -80},
                        {"type": "rel_volume", "k": 100, "window": 5},
                    ],
                },
                flat={"logic": "OR", "conditions": [{"type": "willr_gte", "period": 14, "threshold": -20}]},
            ),
            start,
            end,
        )
        below_ma = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "willr_lte"},
                        {"type": "price_gt_ma", "window": 5},
                    ],
                }
            ),
            start,
            end,
        )
    finally:
        con.close()
    assert alone["trades"]
    assert quiet["trades"] == []
    assert quiet["open_position"] is None
    # The oversold closes (92 and 81) are below SMA(5). The volume-free book still enters.
    assert below_ma["trades"] == []
    assert below_ma["open_position"] is None


def test_trail_still_exits_a_willr_long_at_the_next_open():
    """The entry stays true. The trail, not a %R exit, is what flattens."""
    start = date(2024, 6, 3)
    rows = [
        (100.0, 100.0, 90.0, 91.0, 1_000),
        (110.0, 120.0, 100.0, 101.0, 1_000),
        (105.0, 105.0, 100.0, 102.0, 1_000),
    ]
    # Period 1 on bar 0: (100−91)/(100−90)×−100 = −90. Bar 1: (120−101)/(120−100)×−100 = −95.
    assert float(williams_r(pd.Series([100.0]), pd.Series([90.0]), pd.Series([91.0]), 1).iloc[0]) == -90.0
    assert float(williams_r(pd.Series([120.0]), pd.Series([100.0]), pd.Series([101.0]), 1).iloc[0]) == -95.0
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=2)
        rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 1, "threshold": -80}]},
            trail_pct=5,
        )
        stopped = _run(con, rules, start, end)
        held = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "willr_lte", "period": 1, "threshold": -80}]}
            ),
            start,
            end,
        )
    finally:
        con.close()
    assert stopped["metrics"]["trade_count"] == 1
    trade = stopped["trades"][0]
    assert trade["side"] == "long"
    assert trade["entry_date"] == _day(start, 1)
    assert trade["entry_price"] == 110.0
    assert trade["entry_price"] != 91.0
    assert trade["exit_date"] == _day(start, 2)
    assert trade["exit_price"] == 105.0
    assert trade["exit_price"] != 101.0
    assert trade["exit_reason"] == "trail"
    assert stopped["open_position"] is None
    assert held["trades"] == []
    assert held["open_position"]["side"] == "long"
    assert held["open_position"]["entry_price"] == 110.0


def test_conflict_of_long_and_short_goes_flat():
    view = SignalView(close=100, mas={}, mas_prev={}, willr={14: -50.0})
    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": -20}]},
            short={"logic": "AND", "conditions": [{"type": "willr_gte", "threshold": -80}]},
        )
    )
    assert condition_true(rules["long"]["conditions"][0], view)
    assert condition_true(rules["short"]["conditions"][0], view)
    assert target_side(rules, view, "long") == "flat"


def test_golden_aapl_willr_is_bit_stable_on_the_default_us_book():
    """Default US costs. The fixture is the whole marked public book."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _willr(), start, end, costs={})
        cached = con.execute("SELECT DISTINCT name FROM indicator_cache").fetchall()
    finally:
        con.close()

    assert result["costs"] == CostModel().to_dict()
    assert result["currency"] == "USD"
    assert result["starting_capital"] == 100_000
    assert result["metrics"]["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][0]["exit_reason"] == "signal"
    assert result["trades"][0]["entry_price"] == 93.0
    assert result["trades"][0]["exit_price"] == 106.0
    assert result["open_position"]["side"] == "long"
    assert result["open_position"]["entry_price"] == 82.0
    assert cached == []
    assert _roundtrip(_locked(result)) == expected["result"]
    assert expected["symbol"] == "AAPL"
    assert expected["period"] == 14
    assert expected["long_threshold"] == -80
    assert expected["exit_threshold"] == -20
    assert expected["costs"] == "us-v1.2-defaults"


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/willr_n14_aapl.json. Not used by pytest."""
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _willr(), start, end, costs={})
    finally:
        con.close()
    payload = {
        "symbol": "AAPL",
        "period": 14,
        "long_threshold": -80,
        "exit_threshold": -20,
        "book": "long willr_lte / flat willr_gte",
        "costs": "us-v1.2-defaults",
        "note": "Williams %R includes today's high and low; next open; US default costs",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "result": _roundtrip(_locked(result)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_api_persists_the_thresholds_and_rejects_a_bad_one():
    start = date(2024, 7, 1)
    rows = public_rows()
    _insert(rows, start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "Williams %R",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "willr_lte"}]},
                flat={"logic": "OR", "conditions": [{"type": "willr_gte"}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [
        {"type": "willr_lte", "period": 14, "threshold": -80.0}
    ]
    assert body["rules"]["flat"]["conditions"] == [
        {"type": "willr_gte", "period": 14, "threshold": -20.0}
    ]
    assert "trail_pct" not in body["rules"]

    run = client.post(
        f"/strategies/{body['id']}/backtest",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(days=len(rows) - 1)).isoformat(),
            "costs": ZERO_COSTS,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["trades"][0]["side"] == "long"
    assert run.json()["trades"][0]["entry_price"] == 93.0
    assert run.json()["trades"][0]["exit_reason"] == "signal"

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad threshold",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "willr_lte", "threshold": 30}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_default_costs_are_unchanged_on_a_willr_backtest():
    start = date(2024, 8, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        result = _run(con, _willr(), start, start + timedelta(days=len(rows) - 1), costs={})
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "signal"
        assert result["trades"][0]["entry_commission"] >= 1
        assert result["trades"][0]["exit_commission"] >= 1
    finally:
        con.close()

    con = _insert([(100, 110, 90, 100, 1000)], symbol="0700.HK", market="HK", start=start)
    try:
        hk = _run(con, _willr(), start, start, costs={}, symbol="0700.HK", market="HK")
        assert hk["costs"] == CostModel().to_dict()
        assert hk["starting_capital"] == 500_000
        assert hk["currency"] == "HKD"
        assert hk["metrics"]["trade_count"] == 0
    finally:
        con.close()


def test_overnight_board_does_not_grow_a_willr_cell():
    cells = grid_cells()
    assert GRID_VERSION == "s1-board-v1"
    assert len(cells) == 25
    for cell in cells:
        for side in ("long", "flat", "short"):
            for cond in cell.rules()[side]["conditions"]:
                assert cond["type"] not in {"willr_lte", "willr_gte"}


def test_docs_and_the_research_panel_expose_willr():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    formulas = (ROOT / "indicators" / "FORMULAS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    prd = (ROOT / "PRD.md").read_text()
    assert 'id: "willr"' in script
    assert "Williams %R(14)" in script
    assert "willr_lte: { period: 14, threshold: -80 }" in script
    assert "willr_gte: { period: 14, threshold: -20 }" in script
    assert '{ type: "willr_lte", period: 14, threshold: -80 }' in script
    assert '{ type: "willr_gte", period: 14, threshold: -20 }' in script
    assert "function isWillrType" in script
    assert "Williams %R ≤ threshold" in script
    assert "Williams %R ≥ threshold" in script
    assert "Williams %R(N)" in html
    assert "N=14" in html
    assert "−80" in html and "−20" in html
    assert "willr_lte" in formulas and "willr_gte" in formulas
    assert "including" in formulas
    assert "s1-board-v1" in formulas
    assert "willr_lte" in readme and "−80" in readme
    assert "Williams %R" in prd
    assert "s1-board-v1" in prd

"""S1-K15: RSI falling for N consecutive bars, and the rising sibling.

`rsi_falling_n` is Wilder RSI(period) strictly below the previous bar's RSI
on each of the last N closes. `rsi_rising_n` is the same test with `>`.
N counts steps, so N=3 compares four RSI values. An equal print breaks the
run. The series is `rsi_wilder`, the same function as `RSI_14`.

That is the slope. It is not a z-score of the RSI level (S1-K16, not
implemented): z subtracts a rolling mean and divides by a rolling standard
deviation. A falling streak can print while RSI is still above that mean
(z > 0). A single drop can put RSI below its mean (z < 0) without N
consecutive falls. It is also not `rsi_lte` / `rsi_gte`.

The public pattern is long on RSI(14) rising for 3 bars and short on
RSI(14) falling for 3 bars. No trail. Fills are the next stored open.
Signal cases use zero costs. The golden reruns the same AAPL tape with
the default US book.
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
from dixon.compute_indicators import (
    compute_named,
    ensure_indicators,
    indicator_map,
    load_ohlc,
    rsi_slope_streak,
    rsi_wilder,
    series_by_date,
    slope_streak_flags,
)
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    RSI_SLOPE_N_DEFAULT,
    RSI_SLOPE_PERIOD_DEFAULT,
    SignalView,
    condition_true,
    signal_requirements,
    target_side,
    validate_rules,
    windows_used,
)
from dixon.strategy_board import grid_cells

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "rsi_slope_n3_aapl.json"
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


def _slope(period=14, n=3):
    """Public pattern. Rising streak longs, falling streak shorts. No trail."""
    return _rules(
        long={
            "logic": "AND",
            "conditions": [{"type": "rsi_rising_n", "period": period, "n": n}],
        },
        short={
            "logic": "AND",
            "conditions": [{"type": "rsi_falling_n", "period": period, "n": n}],
        },
    )


def _falling_long(period=14, n=3, extra=None):
    conds = [{"type": "rsi_falling_n", "period": period, "n": n}]
    if extra:
        conds.extend(extra)
    return _rules(long={"logic": "AND", "conditions": conds})


def _rally_then_three_down_ticks():
    """Rally, then three 0.25 down closes. Index 57 is an N=3 fall above the mean."""
    px = 100.0
    out: list[float] = []
    for i in range(40):
        px += 1.5 if i % 2 == 0 else -1.0
        out.append(px)
    for _ in range(15):
        px += 1.0
        out.append(px)
    for _ in range(3):
        px -= 0.25
        out.append(px)
    return pd.Series(out, dtype=float)


def period_split_closes():
    """A short walk where RSI(2) completes a 3-bar fall and RSI(14) does not.

    RSI(2) is warm early. RSI(14) is still warming up at that fall (indexes
    14 and 15), so the two periods are not the same book. Both later rise
    together at index 18.
    """
    return [
        100.0, 101.8, 103.3, 102.98, 104.6, 104.08, 104.06, 105.1, 106.8,
        106.52, 105.46, 105.62, 104.04, 103.36, 102.38, 102.14, 102.56,
        103.88, 104.26, 103.3, 101.88,
    ]


def slope_closes():
    """Chop, a rally, a decline, then another rally.

    Steps are dyadic (0.5 and 1) so the closes are exact in float. The chop
    keeps both Wilder averages alive, so a later one-way run does not pin
    RSI at 0 or 100 on the first bar. 40 + 15 + 15 + 12 = 82 closes.
    """
    px = 100.0
    out: list[float] = []
    for i in range(40):
        px = px + (1.5 if i % 2 == 0 else -1.0)
        out.append(px)
    for _ in range(15):
        px = px + 1.0
        out.append(px)
    for _ in range(15):
        px = px - 1.0
        out.append(px)
    for _ in range(12):
        px = px + 0.5
        out.append(px)
    assert len(out) == 82
    return out


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


def _from_closes(closes, volume=1_000_000):
    """Open is the previous close. High and low wick half a point off the close."""
    rows = []
    prev = float(closes[0])
    for close in closes:
        px = float(close)
        open_ = prev
        rows.append((open_, max(open_, px) + 0.5, min(open_, px) - 0.5, px, volume))
        prev = px
    return rows


def _bump_opens(rows, bump):
    """Shift opens without touching closes, so the fill price is not the signal close."""
    out = []
    for open_, high, low, close, volume in rows:
        opened = float(open_) + bump
        out.append((opened, max(float(high), opened), min(float(low), opened), close, volume))
    return out


def _run(con, rules, start, end, costs=None, symbol="AAPL", market="US"):
    return run_backtest(
        con,
        {
            "id": "k15",
            "name": "rsi slope",
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


def _oracle(values, n, *, falling):
    """Independent streak: N strict steps ending at i, else NaN."""
    flags = []
    for i in range(len(values)):
        ok = i >= n
        if ok:
            for k in range(n):
                cur = values.iloc[i - k]
                prev = values.iloc[i - k - 1]
                if pd.isna(cur) or pd.isna(prev):
                    ok = False
                    break
                if falling and not (cur < prev):
                    ok = False
                    break
                if not falling and not (cur > prev):
                    ok = False
                    break
        flags.append(1.0 if ok else np.nan)
    return flags


def _same_flags(got: pd.Series, expect) -> None:
    assert len(got) == len(expect)
    for i, (left, right) in enumerate(zip(got.tolist(), expect)):
        if pd.isna(right):
            assert pd.isna(left), i
        else:
            assert left == right, i


def _true_idx(flags: pd.Series) -> list[int]:
    return [i for i, value in enumerate(flags.tolist()) if value == 1.0]


def _expected_fills(rising: pd.Series, falling: pd.Series):
    """Next-open side changes. A signal on the last bar does not fill."""
    side = "flat"
    fills: list[tuple[int, str]] = []
    last = len(rising) - 1
    for i in range(last):
        rise = rising.iloc[i] == 1.0
        fall = falling.iloc[i] == 1.0
        if rise and fall:
            target = "flat"
        elif rise:
            target = "long"
        elif fall:
            target = "short"
        else:
            target = side
        if target != side:
            fills.append((i + 1, target))
            side = target
    return fills


def _entry_side(result, side):
    for trade in result["trades"]:
        if trade["side"] == side:
            return trade
    opened = result["open_position"]
    if opened is not None and opened["side"] == side:
        return opened
    raise AssertionError(side)


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


def _zscore(rsi: pd.Series, window: int, ddof: int) -> pd.Series:
    """Test-only RSI z-score. Not a strategy predicate. S1-K16 stays unimplemented."""
    mu = rsi.rolling(window, min_periods=window).mean()
    sigma = rsi.rolling(window, min_periods=window).std(ddof=ddof)
    return (rsi - mu) / sigma.mask(sigma == 0)


def test_streak_operator_matches_an_independent_step_count_and_rejects_ties():
    tied = pd.Series([10.0, 9.0, 9.0, 8.0, 7.0])
    _same_flags(slope_streak_flags(tied, 1, falling=True), _oracle(tied, 1, falling=True))
    _same_flags(slope_streak_flags(tied, 2, falling=True), _oracle(tied, 2, falling=True))
    # 9, 9 is not a fall. The run 8 then 7 is two falls, so N=2 turns true only at the end.
    assert _true_idx(slope_streak_flags(tied, 1, falling=True)) == [1, 3, 4]
    assert _true_idx(slope_streak_flags(tied, 2, falling=True)) == [4]
    assert _true_idx(slope_streak_flags(tied, 1, falling=False)) == []

    broken = pd.Series([5.0, 4.0, np.nan, 3.0, 2.0])
    _same_flags(slope_streak_flags(broken, 2, falling=True), _oracle(broken, 2, falling=True))
    assert _true_idx(slope_streak_flags(broken, 2, falling=True)) == []
    assert _true_idx(slope_streak_flags(broken, 1, falling=True)) == [1, 4]

    close = pd.Series(slope_closes())
    rsi = rsi_wilder(close, 14)
    for n, falling in ((1, True), (3, True), (1, False), (3, False)):
        _same_flags(
            slope_streak_flags(rsi, n, falling=falling),
            _oracle(rsi, n, falling=falling),
        )
        pd.testing.assert_series_equal(
            rsi_slope_streak(close, 14, n, falling=falling),
            slope_streak_flags(rsi, n, falling=falling),
        )
    pd.testing.assert_series_equal(compute_named(close, "RSI_14"), rsi_wilder(close, 14))
    # One series cannot both rise and fall on the same bar.
    rise = slope_streak_flags(rsi, 3, falling=False)
    fall = slope_streak_flags(rsi, 3, falling=True)
    assert not (set(_true_idx(rise)) & set(_true_idx(fall)))


def test_falling_streak_is_not_an_rsi_zscore():
    """Hand RSI levels. The operator never reads a mean or a standard deviation.

    Index 23 (74) is the third consecutive drop from 80, so N=3 falling is
    true, and 74 is still above the prior 20-bar mean (z > 0). Index 20 of
    the crash series is a single drop to 30: z < 0 for both variance
    conventions, and N=3 falling is false. Index 27 (46) is a 3-step rise
    while the level is still below its 20-bar mean (z < 0).
    """
    levels = pd.Series([50.0] * 20 + [80.0, 78.0, 76.0, 74.0, 40.0, 42.0, 44.0, 46.0])
    falling = slope_streak_flags(levels, 3, falling=True)
    rising = slope_streak_flags(levels, 3, falling=False)
    assert float(levels.iloc[23]) == 74.0
    assert falling.iloc[23] == 1.0
    assert pd.isna(rising.iloc[23])
    mean_23 = float(levels.iloc[4:24].mean())
    assert float(levels.iloc[23]) > mean_23
    assert float(levels.iloc[27]) == 46.0
    assert rising.iloc[27] == 1.0
    assert pd.isna(falling.iloc[27])
    mean_27 = float(levels.iloc[8:28].mean())
    assert float(levels.iloc[27]) < mean_27
    for ddof in (0, 1):
        z = _zscore(levels, 20, ddof)
        assert float(z.iloc[23]) > 0
        assert float(z.iloc[27]) < 0

    crash = pd.Series([60.0] * 20 + [30.0])
    assert _true_idx(slope_streak_flags(crash, 3, falling=True)) == []
    assert _true_idx(slope_streak_flags(crash, 1, falling=True)) == [20]
    for ddof in (0, 1):
        assert float(_zscore(crash, 20, ddof).iloc[20]) < 0

    # Same facts on Wilder RSI, not only on a hand-built level series.
    # Three small down closes after the rally complete N=3 while RSI(14)
    # is still above its own 20-bar mean, so z > 0. That bar is also above 5,
    # so `rsi_lte` with threshold 5 is false there.
    dipped = _rally_then_three_down_ticks()
    rsi = rsi_wilder(dipped, 14)
    fall = rsi_slope_streak(dipped, 14, 3, falling=True)
    assert _true_idx(fall) == [57]
    assert float(rsi.iloc[57]) > float(rsi.iloc[38:58].mean())
    assert float(rsi.iloc[57]) > 5
    for ddof in (0, 1):
        assert float(_zscore(rsi, 20, ddof).iloc[57]) > 0

    # The public tape's second rally rises for 3 bars while RSI is still
    # below that mean (z < 0). Other bars have z < 0 without a 3-bar fall.
    tape = pd.Series(slope_closes(), dtype=float)
    tape_rsi = rsi_wilder(tape, 14)
    tape_fall = set(_true_idx(rsi_slope_streak(tape, 14, 3, falling=True)))
    tape_rise = _true_idx(rsi_slope_streak(tape, 14, 3, falling=False))
    mu = tape_rsi.rolling(20, min_periods=20).mean()
    rise_below = [
        i
        for i in tape_rise
        if not pd.isna(mu.iloc[i]) and float(tape_rsi.iloc[i]) < float(mu.iloc[i])
    ]
    assert rise_below
    for ddof in (0, 1):
        z = _zscore(tape_rsi, 20, ddof)
        assert all(float(z.iloc[i]) < 0 for i in rise_below)
        assert any(
            not pd.isna(z.iloc[i]) and float(z.iloc[i]) < 0 and i not in tape_fall
            for i in range(len(tape_rsi))
        )


def test_flat_tape_and_a_pinned_rally_do_not_complete_three_rising_bars():
    flat = pd.Series([100.0] * 30)
    rsi = rsi_wilder(flat, 14)
    assert _true_idx(rsi_slope_streak(flat, 14, 3, falling=True)) == []
    assert _true_idx(rsi_slope_streak(flat, 14, 3, falling=False)) == []
    # Once both averages are zero, RSI is the flat special case, 50.
    assert float(rsi.iloc[20]) == 50.0

    # The first up bar off a flat tape pins RSI at 100. Later up bars stay at 100.
    rally = pd.Series([100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0])
    pinned = rsi_wilder(rally, 14)
    assert float(pinned.iloc[20]) == 100.0
    assert float(pinned.iloc[24]) == 100.0
    assert _true_idx(rsi_slope_streak(rally, 14, 3, falling=False)) == []
    assert _true_idx(rsi_slope_streak(rally, 14, 1, falling=False)) == [20]


def test_cached_rsi_matches_the_streaks_wilder_input():
    closes = slope_closes()
    con = _insert(_from_closes(closes))
    try:
        ensure_indicators(con, "US", "AAPL", "daily", ["RSI_14"])
        bars = load_ohlc(con, "US", "AAPL", "daily")
        cached = indicator_map(con, "US", "AAPL", "daily", "RSI_14")
        direct = series_by_date(bars["bar_date"], rsi_wilder(bars["close"], 14))
        assert cached == direct
        assert len(cached) > 0
    finally:
        con.close()


def test_omitted_knobs_default_to_period_14_and_n_3_and_do_not_rewrite_old_rules():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 20}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 20}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert "trail_pct" not in validate_rules(legacy)
    assert signal_requirements(validate_rules(legacy))["rsi_falling"] == []
    assert signal_requirements(validate_rules(legacy))["rsi_rising"] == []

    rules = validate_rules(_slope())
    assert rules["long"]["conditions"] == [{"type": "rsi_rising_n", "period": 14, "n": 3}]
    assert rules["short"]["conditions"] == [{"type": "rsi_falling_n", "period": 14, "n": 3}]
    assert rules["flat"]["conditions"] == []
    assert "trail_pct" not in rules
    assert "threshold" not in rules["short"]["conditions"][0]
    assert windows_used(rules) == []
    reqs = signal_requirements(rules)
    assert reqs["rsi_falling"] == [[14, 3]]
    assert reqs["rsi_rising"] == [[14, 3]]
    assert reqs["rsi_periods"] == []

    omitted = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "rsi_rising_n"}]},
            short={"logic": "AND", "conditions": [{"type": "rsi_falling_n"}]},
        )
    )
    assert omitted["long"]["conditions"][0]["period"] == RSI_SLOPE_PERIOD_DEFAULT == 14
    assert omitted["short"]["conditions"][0]["n"] == RSI_SLOPE_N_DEFAULT == 3
    assert RSI_SLOPE_PERIOD_DEFAULT != 2

    swept = validate_rules(_slope(period=2, n=1))
    assert signal_requirements(swept)["rsi_falling"] == [[2, 1]]
    assert signal_requirements(swept)["rsi_rising"] == [[2, 1]]
    both = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "rsi_falling_n", "period": 14, "n": 3},
                    {"type": "rsi_falling_n", "period": 2, "n": 5},
                    {"type": "rsi_lte", "period": 14, "threshold": 30},
                ],
            }
        )
    )
    assert signal_requirements(both)["rsi_falling"] == [[2, 5], [14, 3]]
    assert signal_requirements(both)["rsi_periods"] == [14]

    for bad, match in (
        ({"type": "rsi_falling_n", "n": 0}, "rsi_falling_n n"),
        ({"type": "rsi_falling_n", "n": 501}, "rsi_falling_n n"),
        ({"type": "rsi_rising_n", "period": 0}, "rsi_rising_n period"),
        ({"type": "rsi_rising_n", "period": 251}, "rsi_rising_n period"),
        ({"type": "rsi_falling_n", "n": True}, "rsi_falling_n n"),
        ({"type": "rsi_falling_n", "n": 3.5}, "rsi_falling_n n"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_rules(_rules(long={"logic": "AND", "conditions": [bad]}))


def test_same_bar_conflict_only_when_the_periods_differ_and_a_quiet_bar_holds():
    quiet = SignalView(close=100, mas={}, mas_prev={})
    falling = {"type": "rsi_falling_n", "period": 14, "n": 3}
    rising = {"type": "rsi_rising_n", "period": 14, "n": 3}
    assert condition_true(falling, quiet) is False
    assert condition_true(rising, quiet) is False

    up = SignalView(close=100, mas={}, mas_prev={}, rsi_rising={(14, 3): 1.0})
    down = SignalView(close=100, mas={}, mas_prev={}, rsi_falling={(14, 3): 1.0})
    rules = validate_rules(_slope())
    assert target_side(rules, up, "flat") == "long"
    assert target_side(rules, quiet, "long") == "long"
    assert target_side(rules, down, "long") == "short"
    assert target_side(rules, quiet, "short") == "short"

    split = SignalView(
        close=100,
        mas={},
        mas_prev={},
        rsi_rising={(14, 3): 1.0},
        rsi_falling={(2, 3): 1.0},
    )
    conflict = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "rsi_rising_n", "period": 14, "n": 3}]},
            short={"logic": "AND", "conditions": [{"type": "rsi_falling_n", "period": 2, "n": 3}]},
        )
    )
    assert target_side(conflict, split, "flat") == "flat"


def test_next_open_book_follows_the_streak_and_ignores_wicks():
    start = GOLDEN_START
    closes = slope_closes()
    series = pd.Series(closes, dtype=float)
    rising = rsi_slope_streak(series, 14, 3, falling=False)
    falling = rsi_slope_streak(series, 14, 3, falling=True)
    fills = _expected_fills(rising, falling)
    assert fills[0][1] == "long"
    assert any(side == "short" for _i, side in fills)
    assert fills[0][0] < len(closes) - 1

    rows = _bump_opens(_from_closes(closes), 0.25)
    con = _insert(rows)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _slope(), start, end)
    finally:
        con.close()

    dates = [_day(start, i) for i, _side in fills]
    trades = result["trades"]
    opened = result["open_position"]
    assert [t["entry_date"] for t in trades] == dates[:-1]
    assert [t["side"] for t in trades] == [side for _i, side in fills[:-1]]
    assert all(t["exit_reason"] == "signal" for t in trades)
    for trade, exit_at in zip(trades, dates[1:]):
        assert trade["exit_date"] == exit_at
    assert opened is not None
    assert opened["side"] == fills[-1][1]
    assert opened["entry_date"] == dates[-1]
    # Fill price is that bar's open, which the bump moved off the signal close.
    first_fill = fills[0][0]
    assert trades[0]["entry_price"] == rows[first_fill][0]
    assert trades[0]["entry_price"] != closes[first_fill - 1]
    assert trades[0]["entry_date"] != _day(start, first_fill - 1)

    # Same closes, deeper wicks, untouched opens: the book is unchanged.
    wicked = []
    for open_, _high, _low, close, volume in _from_closes(closes):
        wicked.append((open_, float(close) + 25.0, max(0.5, float(close) - 25.0), close, volume))
    assert [row[1] for row in wicked] != [row[1] for row in _from_closes(closes)]
    con = _insert(wicked, symbol="MSFT")
    try:
        end = start + timedelta(days=len(wicked) - 1)
        other = _run(con, _slope(), start, end, symbol="MSFT")
    finally:
        con.close()
    # Opens were not bumped on this tape, so prices differ from the bumped book.
    # They match a clean-open run of the same closes.
    con = _insert(_from_closes(closes), symbol="SPY")
    try:
        clean = _run(con, _slope(), start, end, symbol="SPY")
    finally:
        con.close()
    assert _spine(other) == _spine(clean)
    assert _spine(other) != _spine(result)


def test_n1_enters_before_n3_and_the_last_bar_does_not_fill():
    start = date(2024, 3, 1)
    closes = slope_closes()
    series = pd.Series(closes, dtype=float)
    rise_1 = _true_idx(rsi_slope_streak(series, 14, 1, falling=False))
    rise_3 = _true_idx(rsi_slope_streak(series, 14, 3, falling=False))
    assert rise_1 and rise_3
    assert rise_1[0] < rise_3[0]

    rows = _from_closes(closes)
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        early = _run(con, _slope(n=1), start, end)
        later = _run(con, _slope(n=3), start, end)
    finally:
        con.close()
    # N=1 can short before it longs. Compare the long entries, which are the rising streaks.
    assert _entry_side(early, "long")["entry_date"] == _day(start, rise_1[0] + 1)
    assert _entry_side(later, "long")["entry_date"] == _day(start, rise_3[0] + 1)
    assert _entry_side(early, "long")["entry_date"] < _entry_side(later, "long")["entry_date"]

    fall_at = _true_idx(rsi_slope_streak(series, 14, 3, falling=True))[0]
    assert fall_at < len(closes) - 1
    short_rows = rows[: fall_at + 1]
    con = _insert(short_rows, symbol="MSFT", start=start)
    try:
        stopped = _run(
            con,
            _falling_long(),
            start,
            start + timedelta(days=len(short_rows) - 1),
            symbol="MSFT",
        )
    finally:
        con.close()
    assert stopped["metrics"]["trade_count"] == 0
    assert stopped["open_position"] is None

    live_rows = rows[: fall_at + 2]
    con = _insert(live_rows, symbol="SPY", start=start)
    try:
        lived = _run(
            con,
            _falling_long(),
            start,
            start + timedelta(days=len(live_rows) - 1),
            symbol="SPY",
        )
    finally:
        con.close()
    assert lived["metrics"]["trade_count"] == 0
    assert lived["open_position"]["side"] == "long"
    assert lived["open_position"]["entry_date"] == _day(start, fall_at + 1)
    assert lived["open_position"]["entry_price"] == live_rows[fall_at + 1][0]


def test_falling_long_is_not_the_rsi_lte_level_and_volume_can_block_it():
    start = date(2024, 4, 1)
    closes = slope_closes()
    series = pd.Series(closes, dtype=float)
    rsi = rsi_wilder(series, 14)
    falling = rsi_slope_streak(series, 14, 3, falling=True)
    signal = _true_idx(falling)[0]
    assert float(rsi.iloc[signal]) > 5

    rows = _from_closes(closes, volume=1_000)
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        plain = _run(con, _falling_long(), start, end)
        gated = _run(
            con,
            _falling_long(extra=[{"type": "rsi_lte", "period": 14, "threshold": 5}]),
            start,
            end,
        )
        quiet = _run(
            con,
            _falling_long(extra=[{"type": "rel_volume", "k": 1.5, "window": 20}]),
            start,
            end,
        )
    finally:
        con.close()

    assert plain["open_position"]["entry_date"] == _day(start, signal + 1) or any(
        t["entry_date"] == _day(start, signal + 1) for t in plain["trades"]
    )
    gated_dates = [t["entry_date"] for t in gated["trades"]]
    if gated["open_position"] is not None:
        gated_dates.append(gated["open_position"]["entry_date"])
    assert _day(start, signal + 1) not in gated_dates
    assert quiet["metrics"]["trade_count"] == 0
    assert quiet["open_position"] is None


def test_period_2_and_period_14_are_different_books():
    """RSI(2) finishes a 3-bar fall while RSI(14) is still warming up."""
    start = date(2024, 5, 1)
    closes = period_split_closes()
    series = pd.Series(closes, dtype=float)
    fast = _true_idx(rsi_slope_streak(series, 2, 3, falling=True))
    slow = _true_idx(rsi_slope_streak(series, 14, 3, falling=True))
    assert fast == [14, 15]
    assert slow == []
    assert _true_idx(rsi_slope_streak(series, 2, 3, falling=False)) == [18]
    assert _true_idx(rsi_slope_streak(series, 14, 3, falling=False)) == [18]
    con = _insert(_from_closes(closes), start=start)
    try:
        end = start + timedelta(days=len(closes) - 1)
        a = _run(con, _slope(period=2), start, end)
        b = _run(con, _slope(period=14), start, end)
    finally:
        con.close()
    assert _spine(a) != _spine(b)
    assert a["trades"][0]["side"] == "short"
    assert a["trades"][0]["entry_date"] == _day(start, 15)
    assert b["trades"] == []
    assert b["open_position"]["side"] == "long"
    assert b["open_position"]["entry_date"] == _day(start, 19)


def test_golden_aapl_rsi_slope_is_bit_stable_on_the_default_us_book():
    """Default US costs. The fixture is the whole marked public book."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    closes = slope_closes()
    con = _insert(_from_closes(closes), start=start)
    try:
        end = start + timedelta(days=len(closes) - 1)
        result = _run(con, _slope(), start, end, costs={})
    finally:
        con.close()

    assert result["costs"] == CostModel().to_dict()
    assert result["currency"] == "USD"
    assert result["starting_capital"] == 100_000
    assert result["metrics"]["trade_count"] == 2
    assert [t["side"] for t in result["trades"]] == ["long", "short"]
    assert result["open_position"] is not None
    assert result["open_position"]["side"] == "long"
    assert all(t["exit_reason"] == "signal" for t in result["trades"])
    assert _roundtrip(_locked(result)) == expected["result"]
    assert expected["symbol"] == "AAPL"
    assert expected["period"] == 14
    assert expected["n"] == 3
    assert expected["costs"] == "us-v1.2-defaults"
    assert expected["note"] == "slope streak, not an RSI z-score"


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/rsi_slope_n3_aapl.json. Not used by pytest."""
    start = GOLDEN_START
    closes = slope_closes()
    con = _insert(_from_closes(closes), start=start)
    try:
        end = start + timedelta(days=len(closes) - 1)
        result = _run(con, _slope(), start, end, costs={})
    finally:
        con.close()
    payload = {
        "symbol": "AAPL",
        "period": 14,
        "n": 3,
        "book": "long rsi_rising_n / short rsi_falling_n",
        "costs": "us-v1.2-defaults",
        "note": "slope streak, not an RSI z-score",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "result": _roundtrip(_locked(result)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_api_persists_the_slope_and_rejects_a_bad_n():
    start = date(2024, 6, 3)
    closes = slope_closes()
    _insert(_from_closes(closes), start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "RSI slope",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "rsi_rising_n"}]},
                short={"logic": "AND", "conditions": [{"type": "rsi_falling_n"}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [{"type": "rsi_rising_n", "period": 14, "n": 3}]
    assert body["rules"]["short"]["conditions"] == [{"type": "rsi_falling_n", "period": 14, "n": 3}]
    assert "trail_pct" not in body["rules"]

    run = client.post(
        f"/strategies/{body['id']}/backtest",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(days=len(closes) - 1)).isoformat(),
            "costs": ZERO_COSTS,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["trades"][0]["side"] == "long"
    assert run.json()["trades"][0]["exit_reason"] == "signal"

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad n",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "rsi_falling_n", "n": 0}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_default_costs_are_unchanged_on_an_rsi_slope_backtest():
    start = date(2024, 7, 1)
    closes = slope_closes()
    con = _insert(_from_closes(closes, volume=1000), start=start)
    try:
        result = _run(con, _slope(), start, start + timedelta(days=len(closes) - 1), costs={})
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "signal"
        assert result["trades"][0]["entry_commission"] >= 1
        assert result["trades"][0]["exit_commission"] >= 1
    finally:
        con.close()

    con = _insert([(100, 100, 100, 100, 1000)], symbol="0700.HK", market="HK", start=start)
    try:
        hk = _run(con, _slope(), start, start, costs={}, symbol="0700.HK", market="HK")
        assert hk["costs"] == CostModel().to_dict()
        assert hk["starting_capital"] == 500_000
        assert hk["currency"] == "HKD"
        assert hk["metrics"]["trade_count"] == 0
    finally:
        con.close()


def test_overnight_board_does_not_grow_an_rsi_streak_cell():
    cells = grid_cells()
    assert len(cells) == 25
    for cell in cells:
        for side in ("long", "flat", "short"):
            for cond in cell.rules()[side]["conditions"]:
                assert cond["type"] not in {"rsi_falling_n", "rsi_rising_n"}


def test_docs_and_the_research_panel_call_the_slope_out_from_a_zscore():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    formulas = (ROOT / "indicators" / "FORMULAS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert 'id: "rsiSlope"' in script
    assert "RSI(14) slope 3" in script
    assert "not a z-score of RSI" in script
    assert "rsi_falling_n: { period: 14, n: 3 }" in script
    assert "rsi_rising_n: { period: 14, n: 3 }" in script
    assert '{ type: "rsi_falling_n", period: 14, n: 3 }' in script
    assert '{ type: "rsi_rising_n", period: 14, n: 3 }' in script
    assert "function isRsiStreakType" in script
    assert "cond-period" in script
    assert "RSI falling N bars" in script
    assert "RSI rising N bars" in script
    assert "RSI falling N" in script
    assert "not a z-score of RSI" in html
    assert "period 14, N=3" in html
    assert "rsi_falling_n" in formulas and "rsi_rising_n" in formulas
    assert "S1-K16" in formulas
    assert "z_t(W)" in formulas
    assert "80, 78, 76, 74" in formulas
    assert "**Not** a z-score of RSI" in readme
    assert "S1-K16" in (ROOT / "PRD.md").read_text()

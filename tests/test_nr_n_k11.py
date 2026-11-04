"""S1-K11: Narrow range N, classic NR7, close-confirm only.

Today's high minus low must be strictly below the minimum high-low of the
previous N-1 bars. The signal bar is one of the N bars. A tie is not NR7.
The range is not true range: the previous close does not widen a gap. It is
not an opening-range breakout and not a hold-N-bars exit.

The public pattern is long on N=7 and flat when the close is above the prior
bar's high (`donchian_high` window 1). Long beats flat, so a bar that is both
still enters. Fills are the next stored open. There is no trail.

Signal cases use zero costs. The golden reruns the same AAPL tape with the
default US book. The overnight s1-board-v1 grid does not grow a cell.
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
from dixon.compute_indicators import bar_range, prior_range_min, rolling_close_min, williams_r
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    NR_N_DEFAULT,
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
FIXTURE = Path(__file__).parent / "fixtures" / "nr7_aapl.json"
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


def _nr(window=7, *, with_exit=True):
    """Public book. Long on NR(N). Flat when close > the prior bar's high."""
    flat = (
        {"logic": "OR", "conditions": [{"type": "donchian_high", "window": 1}]}
        if with_exit
        else {"logic": "AND", "conditions": []}
    )
    return _rules(
        long={"logic": "AND", "conditions": [{"type": "nr_n", "window": window}]},
        flat=flat,
    )


def public_rows():
    """Ten AAPL bars for the NR7 public book.

    Bars 0–5 have range 4. Bar 6 has range 1, so it is NR7, and its close
    10.2 is not above the prior high 12, so only the long fires. The fill is
    bar 7's open, 11, not the signal close 10.2. Bar 7's close stays under
    the prior high, so the long is held. Bar 8 closes at 15, above the prior
    high 14, and the exit fills at bar 9's open, 13, not at 15.
    """
    rows: list[tuple[float, float, float, float, int]] = []
    for _ in range(6):
        rows.append((10.0, 12.0, 8.0, 10.0, 1_000_000))
    rows.append((10.0, 10.5, 9.5, 10.2, 1_000_000))
    rows.append((11.0, 14.0, 10.4, 10.45, 1_000_000))
    rows.append((10.5, 16.0, 10.0, 15.0, 1_000_000))
    rows.append((13.0, 16.0, 12.0, 14.0, 1_000_000))
    assert len(rows) == 10
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
            "id": "k11",
            "name": "narrow range",
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


def _ranges(rows):
    return [high - low for _open, high, low, _close, _volume in rows]


def _oracle(high, low, window):
    """Independent strict rank. True only when today's range beats every prior bar."""
    out = []
    h = list(high)
    lo = list(low)
    n = int(window)
    for i in range(len(h)):
        if i < n - 1:
            out.append(False)
            continue
        bad = False
        rngs = []
        for j in range(i - n + 1, i + 1):
            if pd.isna(h[j]) or pd.isna(lo[j]) or h[j] < lo[j]:
                bad = True
                break
            rngs.append(float(h[j]) - float(lo[j]))
        if bad:
            out.append(False)
            continue
        today = rngs[-1]
        out.append(True if n == 1 else today < min(rngs[:-1]))
    return out


def _flags(high, low, window):
    rng = bar_range(pd.Series(high, dtype=float), pd.Series(low, dtype=float))
    if int(window) <= 1:
        return [bool(v == v and v >= 0) for v in rng.tolist()]
    prior = prior_range_min(pd.Series(high, dtype=float), pd.Series(low, dtype=float), window)
    flags = []
    for cur, level in zip(rng.tolist(), prior.tolist()):
        if cur != cur or level != level:
            flags.append(False)
        else:
            flags.append(float(cur) < float(level))
    return flags


def _true_idx(flags):
    return [i for i, flag in enumerate(flags) if flag]


def test_formula_matches_an_independent_strict_rank_and_rejects_true_range():
    rows = public_rows()
    high = [r[1] for r in rows]
    low = [r[2] for r in rows]
    expect = _oracle(high, low, 7)
    assert _flags(high, low, 7) == expect
    assert _true_idx(expect) == [6]
    assert _ranges(rows)[6] == 1.0
    assert min(_ranges(rows)[:6]) == 4.0

    # A gap day can be the narrow high-low and the wide true range.
    gap_high = [10.0, 30.0]
    gap_low = [0.0, 29.0]
    gap_close = [5.0, 29.5]
    tr = []
    prev_close = None
    for h, lo, c in zip(gap_high, gap_low, gap_close):
        span = h - lo
        if prev_close is None:
            tr.append(span)
        else:
            tr.append(max(span, abs(h - prev_close), abs(lo - prev_close)))
        prev_close = c
    assert tr == [10.0, 25.0]
    assert _oracle(gap_high, gap_low, 2) == [False, True]
    assert tr[1] > tr[0]


def test_a_tie_is_not_narrow_and_a_zero_range_is():
    high = [12.0] * 6 + [11.0, 11.0]
    low = [8.0] * 6 + [9.0, 9.0]
    assert _ranges(list(zip([0] * 8, high, low, [0] * 8, [0] * 8))) == [4, 4, 4, 4, 4, 4, 2, 2]
    flags = _oracle(high, low, 7)
    assert flags[6] is True
    assert flags[7] is False
    assert _flags(high, low, 7) == flags

    flat_high = [20.0, 5.0]
    flat_low = [0.0, 5.0]
    assert _oracle(flat_high, flat_low, 2) == [False, True]
    wr = williams_r(
        pd.Series(flat_high), pd.Series(flat_low), pd.Series([10.0, 5.0]), 1
    )
    assert pd.isna(wr.iloc[1])
    assert _oracle(flat_high, flat_low, 1) == [True, True]


def test_a_hole_blocks_the_window_that_contains_it():
    high = [5.0, 12.0, 12.0, 12.0, 12.0, 12.0, 12.0, 11.0]
    low = [9.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 10.0]
    assert high[0] < low[0]
    flags = _oracle(high, low, 7)
    assert flags[6] is False
    assert flags[7] is True
    assert _flags(high, low, 7) == flags


def test_nr4_fires_where_nr7_does_not():
    high = [11.0, 15.0, 15.0, 15.0, 15.0, 15.0, 12.0]
    low = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    assert _oracle(high, low, 7)[6] is False
    assert _oracle(high, low, 4)[6] is True


def test_close_and_volume_do_not_enter_and_close_lowest_disagrees():
    high = [12.0] * 7
    low = [8.0] * 7
    close_b = [10.0] * 6 + [1.0]
    assert bar_range(pd.Series(high), pd.Series(low)).tolist() == [4.0] * 7
    assert prior_range_min(pd.Series(high), pd.Series(low), 7).iloc[-1] == 4.0
    assert _oracle(high, low, 7)[-1] is False
    mins = rolling_close_min(pd.Series(close_b, dtype=float), 7)
    assert float(close_b[-1]) == float(mins.iloc[-1])
    assert close_b[-1] < min(close_b[:-1])


def test_condition_is_strict_and_window_one_is_every_finite_range():
    narrow = {"type": "nr_n", "window": 7}
    view = SignalView(close=10, mas={}, mas_prev={}, bar_range=1.0, range_prior_min={7: 4.0})
    assert condition_true(narrow, view)
    tied = SignalView(close=10, mas={}, mas_prev={}, bar_range=4.0, range_prior_min={7: 4.0})
    assert not condition_true(narrow, tied)
    wider = SignalView(close=10, mas={}, mas_prev={}, bar_range=5.0, range_prior_min={7: 4.0})
    assert not condition_true(narrow, wider)
    missing = SignalView(close=10, mas={}, mas_prev={}, bar_range=None, range_prior_min={7: 4.0})
    assert not condition_true(narrow, missing)
    hole = SignalView(close=10, mas={}, mas_prev={}, bar_range=1.0, range_prior_min={7: None})
    assert not condition_true(narrow, hole)
    one = {"type": "nr_n", "window": 1}
    assert condition_true(one, SignalView(close=10, mas={}, mas_prev={}, bar_range=0.0))
    assert condition_true(one, SignalView(close=10, mas={}, mas_prev={}, bar_range=9.0))
    assert not condition_true(one, SignalView(close=10, mas={}, mas_prev={}, bar_range=None))


def test_omitted_window_is_seven_and_bounds_reject():
    rules = validate_rules(_nr())
    assert rules["long"]["conditions"] == [{"type": "nr_n", "window": 7}]
    assert rules["flat"]["conditions"] == [{"type": "donchian_high", "window": 1}]
    assert "trail_pct" not in rules
    assert windows_used(rules) == []
    assert signal_requirements(rules)["nr_windows"] == [7]
    assert signal_requirements(rules)["donchian_high_windows"] == [1]
    assert NR_N_DEFAULT == 7

    bare = validate_rules(
        _rules(long={"logic": "AND", "conditions": [{"type": "nr_n"}]})
    )
    assert bare["long"]["conditions"] == [{"type": "nr_n", "window": 7}]

    swept = validate_rules(
        _rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 4.0}]})
    )
    assert swept["long"]["conditions"] == [{"type": "nr_n", "window": 4}]

    legacy = _rules(long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 20}]})
    assert signal_requirements(validate_rules(legacy))["nr_windows"] == []

    with pytest.raises(ValueError, match="nr_n window"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 0}]}))
    with pytest.raises(ValueError, match="nr_n window"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 501}]}))
    with pytest.raises(ValueError, match="nr_n window"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": True}]}))
    with pytest.raises(ValueError, match="nr_n window"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 7.5}]}))


def test_next_open_book_fills_the_narrow_bar_and_exits_on_the_prior_high():
    start = date(2024, 3, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _nr(), start, end)
    finally:
        con.close()

    closed, opened = _spine(result)
    assert opened is None
    assert closed == [("long", _day(start, 7), 11.0, _day(start, 9), 13.0, "signal")]
    assert result["trades"][0]["entry_price"] != 10.2
    assert result["trades"][0]["exit_price"] != 15.0
    assert result["equity_curve"][6]["side"] == "flat"
    assert result["equity_curve"][7]["side"] == "long"
    assert result["equity_curve"][8]["side"] == "long"
    assert result["equity_curve"][9]["side"] == "flat"


def test_history_before_the_window_still_ranks_and_the_last_bar_does_not_fill():
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        late = _run(con, _nr(), start + timedelta(days=6), end)
    finally:
        con.close()
    closed, opened = _spine(late)
    assert opened is None
    assert closed == [("long", _day(start, 7), 11.0, _day(start, 9), 13.0, "signal")]

    tail = [(10.0, 12.0, 8.0, 10.0, 1000)] * 6 + [(10.0, 10.5, 9.5, 10.2, 1000)]
    con = _insert(tail, symbol="MSFT", start=start)
    try:
        cold = _run(con, _nr(with_exit=False), start, start + timedelta(days=len(tail) - 1), symbol="MSFT")
    finally:
        con.close()
    assert cold["trades"] == []
    assert cold["open_position"] is None


def test_a_tie_does_not_enter_and_long_beats_a_same_bar_prior_high():
    start = date(2024, 4, 1)
    tied = [(10.0, 12.0, 8.0, 10.0, 1000)] * 6
    tied.append((10.0, 11.0, 9.0, 10.0, 1000))
    tied.append((10.0, 11.0, 9.0, 10.0, 1000))
    tied.append((12.0, 13.0, 11.0, 12.0, 1000))
    con = _insert(tied, start=start)
    try:
        end = start + timedelta(days=len(tied) - 1)
        result = _run(con, _nr(with_exit=False), start + timedelta(days=7), end)
    finally:
        con.close()
    assert result["trades"] == []
    assert result["open_position"] is None

    both = [(10.0, 12.0, 8.0, 10.0, 1000)] * 6
    both.append((12.0, 13.0, 12.0, 12.5, 1000))
    both.append((11.0, 14.0, 10.0, 11.0, 1000))
    con = _insert(both, symbol="MSFT", start=start)
    try:
        entered = _run(con, _nr(), start, start + timedelta(days=len(both) - 1), symbol="MSFT")
    finally:
        con.close()
    _closed, opened = _spine(entered)
    assert _closed == []
    assert opened == ("long", _day(start, 7), 11.0)
    assert opened[2] != 12.5


def test_nr4_and_nr7_are_different_books():
    start = date(2024, 5, 1)
    rows = [
        (10.0, 11.0, 10.0, 10.5, 1000),
        (10.0, 15.0, 10.0, 12.0, 1000),
        (10.0, 15.0, 10.0, 12.0, 1000),
        (10.0, 15.0, 10.0, 12.0, 1000),
        (10.0, 15.0, 10.0, 12.0, 1000),
        (10.0, 15.0, 10.0, 12.0, 1000),
        (10.0, 12.0, 10.0, 11.0, 1000),
        (9.0, 16.0, 8.0, 14.0, 1000),
    ]
    assert _oracle([r[1] for r in rows], [r[2] for r in rows], 4)[6] is True
    assert _oracle([r[1] for r in rows], [r[2] for r in rows], 7)[6] is False
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        short_n = _run(con, _nr(4), start, end)
        long_n = _run(con, _nr(7), start, end)
    finally:
        con.close()
    assert short_n["trades"] == []
    assert short_n["open_position"]["entry_date"] == _day(start, 7)
    assert short_n["open_position"]["entry_price"] == 9.0
    assert long_n["trades"] == []
    assert long_n["open_position"] is None


def test_open_close_and_volume_do_not_move_the_fill():
    start = date(2024, 5, 15)
    base = [(10.0, 12.0, 8.0, 10.0, 1000)] * 6
    base.append((10.0, 10.5, 9.5, 10.0, 1000))
    base.append((11.0, 14.0, 10.0, 12.0, 1000))
    bumped = list(base)
    bumped[6] = (50.0, 10.5, 9.5, 1.0, 9_000_000)
    con = _insert(base, start=start)
    try:
        end = start + timedelta(days=len(base) - 1)
        left = _run(con, _nr(with_exit=False), start, end)
    finally:
        con.close()
    con = _insert(bumped, symbol="MSFT", start=start)
    try:
        right = _run(con, _nr(with_exit=False), start, end, symbol="MSFT")
    finally:
        con.close()
    assert _spine(left) == _spine(right)
    assert left["open_position"]["entry_price"] == 11.0
    assert left["open_position"]["entry_price"] != 1.0


def test_constant_volume_blocks_an_and_with_rel_volume():
    start = date(2024, 6, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        alone = _run(con, _nr(), start, end)
        gated = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "nr_n", "window": 7},
                        {"type": "rel_volume", "k": 1.5, "window": 20},
                    ],
                },
                flat={"logic": "OR", "conditions": [{"type": "donchian_high", "window": 1}]},
            ),
            start,
            end,
        )
    finally:
        con.close()
    assert alone["trades"]
    assert gated["trades"] == []
    assert gated["open_position"] is None


def test_trail_still_exits_an_nr_long_at_the_next_open():
    start = date(2024, 6, 15)
    rows = [
        (100.0, 101.0, 99.0, 100.0, 1000),
        (110.0, 120.0, 100.0, 100.0, 1000),
        (105.0, 106.0, 104.0, 105.0, 1000),
    ]
    rules = _rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 1}]})
    rules["trail_pct"] = 5
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        stopped = _run(con, rules, start, end)
        held = _run(
            con,
            _rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 1}]}),
            start,
            end,
        )
    finally:
        con.close()
    trade = stopped["trades"][0]
    assert trade["side"] == "long"
    assert trade["entry_date"] == _day(start, 1)
    assert trade["entry_price"] == 110.0
    assert trade["exit_date"] == _day(start, 2)
    assert trade["exit_price"] == 105.0
    assert trade["exit_price"] != 100.0
    assert trade["exit_reason"] == "trail"
    assert stopped["open_position"] is None
    assert held["open_position"]["side"] == "long"
    assert held["open_position"]["entry_price"] == 110.0


def test_conflict_of_long_and_short_goes_flat():
    view = SignalView(close=10, mas={}, mas_prev={}, bar_range=1.0, range_prior_min={7: 4.0})
    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 7}]},
            short={"logic": "AND", "conditions": [{"type": "nr_n", "window": 7}]},
        )
    )
    assert target_side(rules, view, "long") == "flat"


def test_golden_aapl_nr7_is_bit_stable_on_the_default_us_book():
    """Default US costs. The fixture is the whole marked public book."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _nr(), start, end, costs={})
        cached = con.execute("SELECT DISTINCT name FROM indicator_cache").fetchall()
    finally:
        con.close()

    assert result["costs"] == CostModel().to_dict()
    assert result["currency"] == "USD"
    assert result["starting_capital"] == 100_000
    assert result["metrics"]["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][0]["exit_reason"] == "signal"
    assert result["trades"][0]["entry_price"] == 11.0
    assert result["trades"][0]["exit_price"] == 13.0
    assert result["open_position"] is None
    assert cached == []
    assert _roundtrip(_locked(result)) == expected["result"]
    assert expected["symbol"] == "AAPL"
    assert expected["window"] == 7
    assert expected["compare"] == "strict"
    assert expected["range"] == "high-low"
    assert expected["costs"] == "us-v1.2-defaults"


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/nr7_aapl.json. Not used by pytest."""
    start = GOLDEN_START
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _nr(), start, end, costs={})
    finally:
        con.close()
    payload = {
        "symbol": "AAPL",
        "window": 7,
        "compare": "strict",
        "range": "high-low",
        "book": "long nr_n / flat donchian_high window 1",
        "costs": "us-v1.2-defaults",
        "note": "NR7 high-low strictly narrowest of 7; exit close > prior high; next open; US default costs",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "result": _roundtrip(_locked(result)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_api_persists_nr7_and_rejects_a_bad_window():
    start = date(2024, 7, 1)
    rows = public_rows()
    _insert(rows, start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "NR7",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "nr_n"}]},
                flat={"logic": "OR", "conditions": [{"type": "donchian_high", "window": 1}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [{"type": "nr_n", "window": 7}]
    assert body["rules"]["flat"]["conditions"] == [{"type": "donchian_high", "window": 1}]
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
    assert run.json()["trades"][0]["entry_price"] == 11.0
    assert run.json()["trades"][0]["exit_price"] == 13.0
    assert run.json()["trades"][0]["exit_reason"] == "signal"

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad window",
            "symbol": "AAPL",
            "rules": _rules(long={"logic": "AND", "conditions": [{"type": "nr_n", "window": 0}]}),
        },
    )
    assert rejected.status_code == 400


def test_default_costs_are_unchanged_on_an_nr7_backtest():
    start = date(2024, 8, 1)
    rows = public_rows()
    con = _insert(rows, start=start)
    try:
        result = _run(con, _nr(), start, start + timedelta(days=len(rows) - 1), costs={})
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "signal"
        assert result["trades"][0]["entry_commission"] >= 1
        assert result["trades"][0]["exit_commission"] >= 1
        assert result["trades"][0]["entry_price"] == 11.0
        assert result["trades"][0]["exit_price"] == 13.0
    finally:
        con.close()

    con = _insert([(100, 110, 90, 100, 1000)], symbol="0700.HK", market="HK", start=start)
    try:
        hk = _run(con, _nr(), start, start, costs={}, symbol="0700.HK", market="HK")
        assert hk["costs"] == CostModel().to_dict()
        assert hk["starting_capital"] == 500_000
        assert hk["currency"] == "HKD"
        assert hk["metrics"]["trade_count"] == 0
    finally:
        con.close()


def test_overnight_board_does_not_grow_an_nr_cell():
    cells = grid_cells()
    assert GRID_VERSION == "s1-board-v1"
    assert len(cells) == 25
    for cell in cells:
        for side in ("long", "flat", "short"):
            for cond in cell.rules()[side]["conditions"]:
                assert cond["type"] != "nr_n"


def test_docs_and_the_research_panel_expose_nr7():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    formulas = (ROOT / "indicators" / "FORMULAS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    prd = (ROOT / "PRD.md").read_text()
    assert 'id: "nr7"' in script
    assert "NR7" in script
    assert "nr_n: { window: 7 }" in script
    assert '{ type: "nr_n", window: 7 }' in script
    assert '{ type: "donchian_high", window: 1 }' in script
    assert "function isNrType" in script
    assert "Narrow range N" in script
    assert "not true range" in script
    assert "opening-range breakout" in script
    assert "NR7" in html
    assert "not true range" in html
    assert "prior bar's high" in html
    assert "nr_n" in formulas
    assert "strict" in formulas
    assert "true range" in formulas.lower() or "true range" in formulas
    assert "s1-board-v1" in formulas
    assert "not true range" in readme
    assert "NR7" in readme
    assert "`nr_n`" in prd
    assert "s1-board-v1" in prd

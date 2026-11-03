"""Connors Double Seven: N-day close extremes plus the SMA(200) long gate.

`close_lowest` / `close_highest` compare the close with the min or max close
of the last N bars, including today. That is not Donchian high/low. The public
pattern is long when the close is the 7-day low and close > SMA(200), and flat
on the 7-day high close. No trail. Fills stay on the next stored open.

Signal cases use zero costs. The golden reruns the same AAPL tape with the
default US book so the P&L, not only the dates, is bit-stable with the gate
on and off.
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
from dixon.compute_indicators import prior_high, prior_low, rolling_close_max, rolling_close_min, sma
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    CLOSE_EXTREME_N_DEFAULT,
    MA_MENU,
    SignalView,
    condition_true,
    signal_requirements,
    target_side,
    validate_rules,
    windows_used,
)

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"
FIXTURE = Path(__file__).parent / "fixtures" / "double_seven_n7_aapl.json"
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


def _double_seven(n=7, gate=True):
    """Public pattern. `gate=False` drops the SMA(200) entry filter."""
    long_conds = [{"type": "close_lowest", "window": n}]
    if gate:
        long_conds.append({"type": "price_gt_ma", "window": 200})
    return _rules(
        long={"logic": "AND", "conditions": long_conds},
        flat={"logic": "OR", "conditions": [{"type": "close_highest", "window": n}]},
    )


def _insert(rows, symbol="AAPL", start=GOLDEN_START):
    con = connect()
    for i, (open_, high, low, close, volume) in enumerate(rows):
        con.execute(
            """
            INSERT INTO daily_bars
              (market, symbol, trade_date, open, high, low, close, volume)
            VALUES ('US', ?, ?, ?, ?, ?, ?, ?)
            """,
            [symbol, start + timedelta(days=i), open_, high, low, close, volume],
        )
    return con


def _run(con, rules, start, end, costs=None, symbol="AAPL"):
    return run_backtest(
        con,
        {
            "id": "k10",
            "name": "double seven",
            "symbol": symbol,
            "market": "US",
            "timeframe": "daily",
            "rules": rules,
        },
        start,
        end,
        ZERO_COSTS if costs is None else costs,
    )


def _day(start, offset):
    return (start + timedelta(days=offset)).isoformat()


def _from_closes(closes, volume=1_000_000):
    """Open is the previous close. High and low wick off the close."""
    rows = []
    prev = float(closes[0])
    for close in closes:
        px = float(close)
        open_ = prev
        rows.append((open_, max(open_, px) + 0.5, min(open_, px) - 0.5, px, volume))
        prev = px
    return rows


def double_seven_closes():
    """Flat 100s, a pop to 110, a 108 close-low above SMA(200), then a 90 low.

    Index 6 is the first 7-day close extreme (the flat stretch: every close
    is both the min and the max, so long wins and an ungated book enters).
    Index 193 is the first 7-day high that is not also the low, so that book
    exits on the next open. Index 199 is a 7-day low above SMA(200). Index
    205 is the matching 7-day high. Index 206 is a 7-day low below SMA(200).
    """
    closes = [100.0] * 193
    closes.extend([110.0] * 6)
    closes.append(108.0)
    closes.extend([109.0] * 6)
    closes.append(90.0)
    closes.append(91.0)
    assert len(closes) == 208
    return closes


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


def test_rolling_close_extreme_includes_today_and_is_not_a_prior_channel():
    close = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0, 2.0, 3.0])
    low = pd.Series([4.0, 3.0, 2.0, 1.0, 0.5, 1.5, 2.5])
    high = pd.Series([6.0, 5.0, 4.0, 3.0, 1.5, 2.5, 4.0])
    assert pd.isna(rolling_close_min(close, 3).iloc[1])
    # Index 4, N=3: closes 3, 2, 1. Today is inside the min.
    assert rolling_close_min(close, 3).iloc[4] == 1
    # Index 6, N=3: closes 1, 2, 3. Today is inside the max.
    assert rolling_close_max(close, 3).iloc[6] == 3
    # Donchian drops today and reads lows/highs, so the same index disagrees.
    assert prior_low(low, 3).iloc[4] == 1
    assert close.iloc[4] == rolling_close_min(close, 3).iloc[4]
    assert not (close.iloc[4] < prior_low(low, 3).iloc[4])
    assert prior_high(high, 3).iloc[6] == 3
    assert close.iloc[6] == rolling_close_max(close, 3).iloc[6]
    assert not (close.iloc[6] > prior_high(high, 3).iloc[6])
    assert CLOSE_EXTREME_N_DEFAULT == 7


def test_tie_counts_for_both_extremes_and_warmup_is_false():
    tied = SignalView(close=10, mas={}, mas_prev={}, close_min={7: 10.0}, close_max={7: 10.0})
    low = {"type": "close_lowest", "window": 7}
    high = {"type": "close_highest", "window": 7}
    assert condition_true(low, tied) is True
    assert condition_true(high, tied) is True
    cold = SignalView(close=10, mas={}, mas_prev={})
    assert condition_true(low, cold) is False
    assert condition_true(high, cold) is False
    rules = validate_rules(_double_seven(gate=False))
    assert target_side(rules, tied, "flat") == "long"
    assert target_side(rules, tied, "long") == "long"


def test_omitted_window_defaults_to_7_and_sma200_joins_the_ma_menu():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert "trail_pct" not in validate_rules(legacy)

    rules = validate_rules(_double_seven())
    assert rules["long"]["conditions"] == [
        {"type": "close_lowest", "window": 7},
        {"type": "price_gt_ma", "window": 200},
    ]
    assert rules["flat"]["conditions"] == [{"type": "close_highest", "window": 7}]
    assert rules["short"]["conditions"] == []
    assert "trail_pct" not in rules
    assert windows_used(rules) == [200]
    reqs = signal_requirements(rules)
    assert reqs["close_low_windows"] == [7]
    assert reqs["close_high_windows"] == [7]
    assert reqs["donchian_high_windows"] == []
    assert reqs["donchian_low_windows"] == []

    omitted = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "close_lowest"}]},
            flat={"logic": "OR", "conditions": [{"type": "close_highest"}]},
        )
    )
    assert omitted["long"]["conditions"][0]["window"] == CLOSE_EXTREME_N_DEFAULT
    assert omitted["flat"]["conditions"][0]["window"] == CLOSE_EXTREME_N_DEFAULT
    assert windows_used(omitted) == []

    swept = validate_rules(_double_seven(n=4))
    assert signal_requirements(swept)["close_low_windows"] == [4]
    assert signal_requirements(swept)["close_high_windows"] == [4]
    assert 200 in MA_MENU
    assert validate_rules(
        _rules(long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 200}]})
    )["long"]["conditions"][0]["window"] == 200

    with pytest.raises(ValueError, match="not in the fixed menu"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 199}]})
        )
    with pytest.raises(ValueError, match="close_lowest window"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "close_lowest", "window": 0}]})
        )
    with pytest.raises(ValueError, match="close_highest window"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "close_highest", "window": 501}]})
        )
    with pytest.raises(ValueError, match="close_lowest window"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "close_lowest", "window": True}]})
        )


def test_n4_and_n7_enter_and_exit_on_the_next_open():
    """N=4 sees the low at index 3. N=7 is not a low until index 7.

    Opens are the previous close. N=4 sells the 4-day high at index 5 (fill
    index 6), then buys the new low at index 7 again. N=7 skips that first
    round trip and holds the shared entry until the 8.5 has aged out of the
    seven-close window.
    """
    start = date(2024, 3, 1)
    closes = [10, 9, 8, 7, 7.5, 8, 8.5, 6, 6.2, 6.4, 6.6, 6.8, 7.0, 7.5, 7.6]
    rows = _from_closes(closes)
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        short_n = _run(con, _double_seven(n=4, gate=False), start, end)
        long_n = _run(con, _double_seven(n=7, gate=False), start, end)
    finally:
        con.close()

    assert short_n["metrics"]["trade_count"] == 2
    assert short_n["open_position"] is None
    first, second = short_n["trades"]
    assert first["side"] == "long"
    assert first["entry_date"] == _day(start, 4)
    assert first["exit_date"] == _day(start, 6)
    assert first["entry_price"] == 7
    assert first["exit_price"] == 8
    assert first["exit_reason"] == "signal"
    assert second["entry_date"] == _day(start, 8)
    assert second["exit_date"] == _day(start, 11)
    assert second["entry_price"] == 6
    assert second["exit_price"] == 6.6

    assert long_n["metrics"]["trade_count"] == 1
    assert long_n["open_position"] is None
    held = long_n["trades"][0]
    assert held["entry_date"] == _day(start, 8)
    assert held["exit_date"] == _day(start, 14)
    assert held["entry_price"] == 6
    assert held["exit_price"] == 7.5
    assert held["exit_reason"] == "signal"
    assert [(t["entry_date"], t["exit_date"]) for t in short_n["trades"]] != [
        (held["entry_date"], held["exit_date"])
    ]


def test_close_extreme_entry_and_exit_differ_from_donchian_on_wicks():
    """Same bars, same N. Highs and lows are not the close extremes.

    Index 7 close is the 7-day closing low, but the prior lows wick to 1, so
    Donchian low is false. Index 14 close is the 7-day closing high, but a
    prior high wicks to 80, so Donchian high is false. The close-extreme book
    trades; the Donchian book does not.
    """
    start = date(2024, 4, 1)
    rows = []
    closes = [30, 28, 26, 24, 22, 21, 23, 18, 19, 20, 21, 22, 23, 24, 30, 30.5]
    for i, close in enumerate(closes):
        if i < 7:
            rows.append((float(close), 100.0, 1.0, float(close), 1000))
        elif i == 7:
            rows.append((20.0, 21.0, 19.0, 18.0, 1000))
        elif i == 8:
            rows.append((18.4, 80.0, 17.0, 19.0, 1000))
        elif i == 15:
            rows.append((30.5, 31.0, 30.0, 30.5, 1000))
        else:
            rows.append((float(close) - 1, float(close) + 1, float(close) - 1, float(close), 1000))

    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    assert (frame["high"] != frame["close"]).any()
    assert (frame["low"] != frame["close"]).any()
    cmin = rolling_close_min(frame["close"], 7)
    cmax = rolling_close_max(frame["close"], 7)
    dhigh = prior_high(frame["high"], 7)
    dlow = prior_low(frame["low"], 7)

    def _eq_flags(level):
        return [
            (not pd.isna(lv)) and float(px) == float(lv)
            for px, lv in zip(frame["close"], level)
        ]

    def _break_flags(level, above):
        flags = []
        for px, lv in zip(frame["close"], level):
            if pd.isna(lv):
                flags.append(False)
            elif above:
                flags.append(float(px) > float(lv))
            else:
                flags.append(float(px) < float(lv))
        return flags

    close_low_flags = _eq_flags(cmin)
    close_high_flags = _eq_flags(cmax)
    don_high_flags = _break_flags(dhigh, above=True)
    don_low_flags = _break_flags(dlow, above=False)
    assert close_low_flags != don_low_flags
    assert close_high_flags != don_high_flags
    assert close_low_flags[7] is True
    assert don_low_flags[7] is False
    assert float(dlow.iloc[7]) == 1
    assert close_high_flags[14] is True
    assert don_high_flags[14] is False
    assert float(dhigh.iloc[14]) == 80

    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        extreme = _run(con, _double_seven(gate=False), start, end)
        channel = _run(
            con,
            _rules(
                long={"logic": "AND", "conditions": [{"type": "donchian_high", "window": 7}]},
                flat={"logic": "OR", "conditions": [{"type": "donchian_low", "window": 7}]},
            ),
            start,
            end,
        )
    finally:
        con.close()

    assert extreme["metrics"]["trade_count"] == 1
    assert extreme["open_position"] is None
    trade = extreme["trades"][0]
    assert trade["entry_date"] == _day(start, 8)
    assert trade["entry_price"] == 18.4
    assert trade["exit_date"] == _day(start, 13)
    assert trade["exit_price"] == 23
    assert trade["exit_reason"] == "signal"
    assert channel["metrics"]["trade_count"] == 0
    assert channel["open_position"] is None
    assert extreme["trades"] != channel["trades"]


def test_sma200_gate_blocks_the_close_low_taken_without_it():
    """Ungated buys the flat stretch and the 90 low. The gate skips both.

    The 108 low (index 199) is above SMA(200), so both books take that trade
    and sell the 109 high close. Costs are zero, so the fill prices are the
    next opens (the previous closes).
    """
    start = GOLDEN_START
    closes = double_seven_closes()
    series = pd.Series(closes)
    assert rolling_close_min(series, 7).iloc[199] == 108
    assert float(series.iloc[199]) > float(sma(series, 200).iloc[199])
    assert rolling_close_min(series, 7).iloc[206] == 90
    assert float(series.iloc[206]) < float(sma(series, 200).iloc[206])
    assert pd.isna(sma(series, 200).iloc[198])

    rows = _from_closes(closes)
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        gated = _run(con, _double_seven(gate=True), start, end)
        ungated = _run(con, _double_seven(gate=False), start, end)
    finally:
        con.close()

    assert gated["metrics"]["trade_count"] == 1
    assert gated["open_position"] is None
    gated_trade = gated["trades"][0]
    assert gated_trade["entry_date"] == _day(start, 200)
    assert gated_trade["exit_date"] == _day(start, 206)
    assert gated_trade["entry_price"] == 108
    assert gated_trade["exit_price"] == 109
    assert gated_trade["exit_reason"] == "signal"
    assert gated_trade["pnl"] == round(20_000 / 108, 6)

    assert ungated["metrics"]["trade_count"] == 2
    assert ungated["trades"][0]["entry_date"] == _day(start, 7)
    assert ungated["trades"][0]["exit_date"] == _day(start, 194)
    assert ungated["trades"][0]["entry_price"] == 100
    assert ungated["trades"][0]["exit_price"] == 110
    assert ungated["trades"][0]["pnl"] == 2000
    assert ungated["trades"][1]["entry_date"] == gated_trade["entry_date"]
    assert ungated["trades"][1]["exit_date"] == gated_trade["exit_date"]
    assert ungated["open_position"]["entry_date"] == _day(start, 207)
    assert ungated["open_position"]["entry_price"] == 90
    assert ungated["open_position"]["side"] == "long"
    assert [t["entry_date"] for t in gated["trades"]] != [t["entry_date"] for t in ungated["trades"]]


def test_golden_aapl_double_seven_is_bit_stable_with_the_gate_on_and_off():
    """Default US costs. The fixture is the whole marked book, gate on and off."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    rows = _from_closes(double_seven_closes())
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        gated = _run(con, _double_seven(gate=True), start, end, costs={})
        ungated = _run(con, _double_seven(gate=False), start, end, costs={})
    finally:
        con.close()

    assert gated["costs"] == CostModel().to_dict()
    assert ungated["costs"] == CostModel().to_dict()
    assert gated["currency"] == "USD"
    assert gated["starting_capital"] == 100_000
    assert gated["metrics"]["trade_count"] == 1
    assert ungated["metrics"]["trade_count"] == 2
    assert gated["open_position"] is None
    assert ungated["open_position"] is not None
    assert gated["trades"] != ungated["trades"]
    assert gated["metrics"]["total_pnl"] != ungated["metrics"]["total_pnl"]
    assert all(t["exit_reason"] == "signal" for t in gated["trades"] + ungated["trades"])
    got = _roundtrip({"gated": _locked(gated), "ungated": _locked(ungated)})
    assert got == {"gated": expected["gated"], "ungated": expected["ungated"]}
    assert expected["symbol"] == "AAPL"
    assert expected["n"] == 7
    assert expected["gate"] == "price_gt_ma:200"


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/double_seven_n7_aapl.json. Not used by pytest."""
    start = GOLDEN_START
    rows = _from_closes(double_seven_closes())
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        gated = _run(con, _double_seven(gate=True), start, end, costs={})
        ungated = _run(con, _double_seven(gate=False), start, end, costs={})
    finally:
        con.close()
    payload = {
        "symbol": "AAPL",
        "n": 7,
        "gate": "price_gt_ma:200",
        "costs": "us-v1.2-defaults",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "gated": _roundtrip(_locked(gated)),
        "ungated": _roundtrip(_locked(ungated)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_api_persists_double_seven_and_rejects_a_bad_window():
    start = date(2024, 8, 1)
    closes = [10, 9, 8, 7, 7.5, 8, 8.5, 6, 6.2, 6.4, 6.6, 6.8, 7.0, 7.5, 7.6]
    _insert(_from_closes(closes), start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "Double Seven",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "close_lowest"}]},
                flat={"logic": "OR", "conditions": [{"type": "close_highest", "window": 7}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [{"type": "close_lowest", "window": 7}]
    assert body["rules"]["flat"]["conditions"] == [{"type": "close_highest", "window": 7}]
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
    assert run.json()["trades"][0]["entry_date"] == _day(start, 8)
    assert run.json()["trades"][0]["exit_date"] == _day(start, 14)
    assert run.json()["trades"][0]["exit_reason"] == "signal"

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad n",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "close_lowest", "window": 0}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_default_costs_are_unchanged_on_a_double_seven_backtest():
    start = date(2024, 9, 2)
    closes = [10, 9, 8, 7, 7.5, 8, 8.5, 6, 6.2, 6.4, 6.6, 6.8, 7.0, 7.5, 7.6]
    con = _insert(_from_closes(closes, volume=1000), start=start)
    try:
        result = _run(
            con,
            _double_seven(gate=False),
            start,
            start + timedelta(days=len(closes) - 1),
            costs={},
        )
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "signal"
        assert result["trades"][0]["entry_commission"] >= 1
        assert result["trades"][0]["exit_commission"] >= 1
    finally:
        con.close()


def test_research_panel_can_author_double_seven():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert 'id: "double7"' in script
    assert "Double Seven" in script
    assert "lowest close of the last 7 sessions" in script
    assert "highest close of the last 7 sessions" in script
    assert "above SMA(200)" in script
    assert "No trailing stop" in script
    assert "not the Donchian" in script
    assert "close_lowest: { window: 7 }" in script
    assert "close_highest: { window: 7 }" in script
    assert '{ type: "price_gt_ma", window: 200 }' in script
    assert "function isCloseExtremeType" in script
    assert "Close-low N" in script
    assert "Close is N-day low" in script
    assert "Close is N-day high" in script
    assert "200" in script.split("const MA_MENU = ", 1)[1].split(";", 1)[0]
    assert "lowest of the last N closes" in html
    assert "SMA(200)" in html
    assert "not the Donchian high or low" in html

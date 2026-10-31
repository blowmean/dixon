"""ROC(period) percent-point predicates and the trail_pct stop.

K2 is ROC(7) ≥ +1% long, ROC(7) ≤ −1% short, and a 5% trail on the public
pattern. The trail is optional: omitted rules stay canonical and do not stop.
Fills stay on the next stored bar's open. Costs in the signal cases are zero
so the assertion is the path, not the commission. One case echoes the PRD
US/HK defaults to show the fee table is untouched.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.compute_indicators import rate_of_change
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    K2_TRAIL_PCT_DEFAULT,
    ROC_GTE_THRESHOLD_DEFAULT,
    ROC_LTE_THRESHOLD_DEFAULT,
    ROC_PERIOD_DEFAULT,
    signal_requirements,
    trail_triggered,
    updated_extreme,
    validate_rules,
    windows_used,
)

STATIC = Path(__file__).resolve().parents[1] / "dixon" / "static"

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


def _insert(rows, symbol="AAPL", start=date(2024, 5, 1)):
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
            "id": "k2",
            "name": "k2",
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


def _k2_long_rows():
    """ROC(7) hits +1% on bar 7. Bar 8 fills and prints the peak. Bar 9 closes through a 5% trail."""
    rows = [(100, 100, 100, 100, 1000) for _ in range(7)]
    rows.append((101, 101, 101, 101, 1000))
    rows.append((101, 120, 101, 119, 1000))
    rows.append((119, 119, 113, 113, 1000))
    rows.append((113, 113, 113, 113, 1000))
    return rows


def test_roc_percent_and_trail_limit_are_percent_points():
    closes = pd.Series([100.0] * 7 + [101.0, 99.0])
    roc = rate_of_change(closes, 7)
    assert pd.isna(roc.iloc[6])
    assert roc.iloc[7] == pytest.approx(1.0)
    assert roc.iloc[8] == pytest.approx(-1.0)
    assert pd.isna(rate_of_change(pd.Series([0.0, 0.0, 5.0]), 2).iloc[2])

    assert updated_extreme("long", 101, high=120, low=100) == 120
    assert updated_extreme("long", 120, high=119, low=113) == 120
    assert updated_extreme("short", 99, high=99, low=80) == 80
    assert updated_extreme("short", 80, high=85, low=81) == 80
    # 5% under 120 is 114. Well clear of the float dust band.
    assert trail_triggered("long", 120, 113, 5) is True
    assert trail_triggered("long", 120, 119, 5) is False
    # 5% above 80 is 84.
    assert trail_triggered("short", 80, 85, 5) is True
    assert trail_triggered("short", 80, 82, 5) is False
    assert K2_TRAIL_PCT_DEFAULT == 5.0


def test_omitted_roc_knobs_default_and_omitted_trail_stays_off():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert "trail_pct" not in validate_rules(legacy)

    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "roc_gte"}]},
            short={"logic": "AND", "conditions": [{"type": "roc_lte"}]},
        )
    )
    assert rules["long"]["conditions"][0] == {
        "type": "roc_gte",
        "period": ROC_PERIOD_DEFAULT,
        "threshold": ROC_GTE_THRESHOLD_DEFAULT,
    }
    assert rules["short"]["conditions"][0] == {
        "type": "roc_lte",
        "period": ROC_PERIOD_DEFAULT,
        "threshold": ROC_LTE_THRESHOLD_DEFAULT,
    }
    assert "trail_pct" not in rules
    assert validate_rules({**rules, "trail_pct": None}) == rules
    assert validate_rules({**_rules(), "trail_pct": 5})["trail_pct"] == 5.0

    mixed = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 20},
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                ],
            }
        )
    )
    assert windows_used(mixed) == [20]
    assert signal_requirements(mixed)["roc_periods"] == [7]

    with pytest.raises(ValueError, match="not in the fixed menu"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 7}]})
        )
    with pytest.raises(ValueError, match="roc_gte period"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "roc_gte", "period": 0}]})
        )
    with pytest.raises(ValueError, match="roc_lte period"):
        validate_rules(
            _rules(short={"logic": "AND", "conditions": [{"type": "roc_lte", "period": 501}]})
        )
    with pytest.raises(ValueError, match="roc_gte threshold"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "roc_gte", "threshold": 10001}]})
        )
    with pytest.raises(ValueError, match="trail_pct"):
        validate_rules({**_rules(), "trail_pct": 0})
    with pytest.raises(ValueError, match="trail_pct"):
        validate_rules({**_rules(), "trail_pct": 100.01})


def test_roc7_long_trails_five_percent_from_the_peak_at_the_next_open():
    """Bar 9 still has ROC(7) ≥ +1%, so only the trail exits. The fill is bar 10's open, not 114."""
    start = date(2024, 5, 1)
    rows = _k2_long_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "roc_gte"}]},
            trail_pct=5,
        )
        stopped = _run(con, rules, start, end)
        assert stopped["metrics"]["trade_count"] == 1
        trade = stopped["trades"][0]
        assert trade["side"] == "long"
        assert trade["entry_date"] == _day(start, 8)
        assert trade["entry_price"] == 101
        assert trade["exit_date"] == _day(start, 10)
        assert trade["exit_price"] == 113
        assert trade["exit_reason"] == "trail"
        assert stopped["open_position"] is None
        shares = 0.20 * 100_000 / 101
        assert trade["shares"] == round(shares, 8)
        assert trade["pnl"] == round((113 - 101) * shares, 6)
        assert stopped["metrics"]["total_pnl"] == trade["pnl"]
        assert stopped["metrics"]["ending_equity"] == round(100_000 + trade["pnl"], 6)

        held = _run(con, _rules(long={"logic": "AND", "conditions": [{"type": "roc_gte"}]}), start, end)
        assert held["metrics"]["trade_count"] == 0
        assert held["open_position"]["side"] == "long"
        assert held["open_position"]["entry_date"] == _day(start, 8)
        assert held["open_position"]["entry_price"] == 101
    finally:
        con.close()


def test_roc7_short_trails_five_percent_from_the_trough():
    start = date(2024, 6, 3)
    rows = [(100, 100, 100, 100, 1000) for _ in range(7)]
    rows.append((99, 99, 99, 99, 1000))
    rows.append((99, 99, 80, 82, 1000))
    rows.append((82, 85, 81, 85, 1000))
    rows.append((86, 86, 86, 86, 1000))
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        rules = _rules(
            short={"logic": "AND", "conditions": [{"type": "roc_lte"}]},
            trail_pct=5,
        )
        stopped = _run(con, rules, start, end)
        trade = stopped["trades"][0]
        assert stopped["metrics"]["trade_count"] == 1
        assert trade["side"] == "short"
        assert trade["entry_date"] == _day(start, 8)
        assert trade["entry_price"] == 99
        assert trade["exit_date"] == _day(start, 10)
        assert trade["exit_price"] == 86
        assert trade["exit_reason"] == "trail"
        shares = 0.20 * 100_000 / 99
        assert trade["pnl"] == round((99 - 86) * shares, 6)

        held = _run(con, _rules(short={"logic": "AND", "conditions": [{"type": "roc_lte"}]}), start, end)
        assert held["metrics"]["trade_count"] == 0
        assert held["open_position"]["side"] == "short"
        assert held["open_position"]["entry_price"] == 99
    finally:
        con.close()


def test_trail_does_not_flatten_on_the_last_bar_or_block_a_reversal():
    start = date(2024, 7, 1)
    rows = [(100, 100, 100, 100, 1000) for _ in range(7)]
    rows.append((101, 101, 101, 101, 1000))
    rows.append((101, 120, 101, 119, 1000))
    rows.append((119, 119, 113, 113, 1000))
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        held = _run(
            con,
            _rules(long={"logic": "AND", "conditions": [{"type": "roc_gte"}]}, trail_pct=5),
            start,
            end,
        )
        assert held["metrics"]["trade_count"] == 0
        assert held["open_position"]["entry_date"] == _day(start, 8)
        assert held["open_position"]["side"] == "long"
    finally:
        con.close()

    start = date(2024, 7, 15)
    rows = [
        (100, 100, 100, 100, 1000),
        (110, 110, 110, 110, 1000),
        (110, 150, 110, 90, 1000),
        (90, 90, 90, 90, 1000),
    ]
    con = _insert(rows, symbol="MSFT", start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "roc_gte", "period": 1, "threshold": 1}]},
            short={"logic": "AND", "conditions": [{"type": "roc_lte", "period": 1, "threshold": -1}]},
            trail_pct=10,
        )
        flipped = _run(con, rules, start, end, symbol="MSFT")
        trade = flipped["trades"][0]
        assert trade["side"] == "long"
        assert trade["exit_reason"] == "signal"
        assert trade["entry_price"] == 110
        assert trade["exit_price"] == 90
        assert trade["exit_date"] == _day(start, 3)
        assert flipped["open_position"]["side"] == "short"
        assert flipped["open_position"]["entry_price"] == 90
        assert flipped["open_position"]["entry_date"] == _day(start, 3)
    finally:
        con.close()


def test_faded_roc_holds_until_the_trail():
    """Inside the ±1% band the side does not fire, so the book keeps the position."""
    start = date(2024, 8, 1)
    rows = [
        (100, 100, 100, 100, 1000),
        (110, 110, 110, 110, 1000),
        (110, 110, 110, 110, 1000),
        (109, 109, 109, 109, 1000),
    ]
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "roc_gte", "period": 1, "threshold": 1}]},
            short={"logic": "AND", "conditions": [{"type": "roc_lte", "period": 1, "threshold": -1}]},
            trail_pct=50,
        )
        held = _run(con, rules, start, end)
        assert held["metrics"]["trade_count"] == 0
        assert held["open_position"]["entry_date"] == _day(start, 2)
        assert held["open_position"]["entry_price"] == 110
        assert held["equity_curve"][-1]["side"] == "long"
    finally:
        con.close()


def test_roc_ands_with_rel_volume_rsi_and_donchian():
    start = date(2024, 9, 2)
    closes = [100] * 7 + [102, 102, 102, 102]
    volumes = [100] * 9 + [1000, 100]
    rows = [(c, c, c, c, v) for c, v in zip(closes, volumes)]
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        gated = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                    {"type": "rel_volume", "k": 1.5, "window": 3},
                ],
            }
        )
        plain = _rules(
            long={"logic": "AND", "conditions": [{"type": "roc_gte", "period": 7, "threshold": 1}]}
        )
        confirmed = _run(con, gated, start, end)
        assert confirmed["metrics"]["trade_count"] == 0
        assert confirmed["open_position"]["entry_date"] == _day(start, 10)
        assert confirmed["open_position"]["entry_price"] == 102

        early = _run(con, plain, start, end)
        assert early["open_position"]["entry_date"] == _day(start, 8)

        rsi_block = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                    {"type": "rsi_lte", "period": 2, "threshold": 5},
                ],
            }
        )
        blocked = _run(con, rsi_block, start, end)
        assert blocked["metrics"]["trade_count"] == 0
        assert blocked["open_position"] is None
    finally:
        con.close()

    start = date(2024, 9, 16)
    rows = [(100, 200, 100, 100, 1000)]
    rows.extend((100, 100, 100, 100, 1000) for _ in range(6))
    rows.append((102, 102, 102, 102, 1000))
    rows.append((102, 102, 102, 102, 1000))
    con = _insert(rows, symbol="MSFT", start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        channel = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                    {"type": "donchian_high", "window": 7},
                ],
            }
        )
        blocked = _run(con, channel, start, end, symbol="MSFT")
        assert blocked["metrics"]["trade_count"] == 0
        assert blocked["open_position"] is None
    finally:
        con.close()


def test_default_costs_are_unchanged_on_a_roc_backtest():
    start = date(2024, 10, 1)
    rows = _k2_long_rows()
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(
            con,
            _rules(long={"logic": "AND", "conditions": [{"type": "roc_gte"}]}, trail_pct=5),
            start,
            end,
            costs={},
        )
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "trail"
        assert result["trades"][0]["exit_commission"] >= 1
        assert result["trades"][0]["entry_commission"] >= 1
    finally:
        con.close()


def test_api_persists_k2_rules_and_rejects_a_zero_trail():
    start = date(2024, 11, 1)
    rows = _k2_long_rows()
    _insert(rows, start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "K2 ROC",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "roc_gte"}]},
                short={"logic": "AND", "conditions": [{"type": "roc_lte"}]},
                trail_pct=5,
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"][0] == {
        "type": "roc_gte",
        "period": 7,
        "threshold": 1.0,
    }
    assert body["rules"]["short"]["conditions"][0] == {
        "type": "roc_lte",
        "period": 7,
        "threshold": -1.0,
    }
    assert body["rules"]["trail_pct"] == 5.0
    assert body["rules"]["flat"]["conditions"] == []

    run = client.post(
        f"/strategies/{body['id']}/backtest",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(days=len(rows) - 1)).isoformat(),
            "costs": ZERO_COSTS,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["trades"][0]["exit_reason"] == "trail"
    assert run.json()["trades"][0]["exit_price"] == 113

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad trail",
            "symbol": "AAPL",
            "rules": {**_rules(), "trail_pct": 0},
        },
    )
    assert rejected.status_code == 400


def test_research_panel_can_author_a_k2_strategy():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert 'id: "roc"' in script
    assert "ROC(7) ±1%" in script
    assert "ROC(7) is at least +1%" in script
    assert "5% trailing stop" in script
    assert "next open" in script
    assert "trail_pct: 5" in script
    assert "roc_gte: { period: 7, threshold: 1 }" in script
    assert "roc_lte: { period: 7, threshold: -1 }" in script
    assert "function isRocType" in script
    assert 'id="stTrailPct"' in html
    assert "Trail % from peak" in html
    assert "1 means +1%" in html

"""EMA(fast)/EMA(slow) stack predicates and the volume-confirmed public pattern.

K3 is long when EMA(9) > EMA(21) and short when EMA(9) < EMA(21), each ANDed
with the existing rel_volume condition (k=1.5, window=20). The inequality is
the stack, true on every bar it holds, not only the crossing bar. Spans are
not SMA-menu windows. A quiet bar does not fire, so the open side is held.
Costs in the signal cases are zero so the assertion is the path. One case
echoes the PRD US/HK defaults to show the fee table is untouched.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.compute_indicators import ema
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    EMA_FAST_DEFAULT,
    EMA_SLOW_DEFAULT,
    SignalView,
    condition_true,
    signal_requirements,
    target_side,
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


def _k3(**overrides):
    """Public pattern: 9/21 stack AND rel_volume on long and on short."""
    long_ema = {"type": "ema_gt_ema", "fast": 9, "slow": 21}
    short_ema = {"type": "ema_lt_ema", "fast": 9, "slow": 21}
    volume = {"type": "rel_volume", "k": 1.5, "window": 20}
    rules = _rules(
        long={"logic": "AND", "conditions": [long_ema, volume]},
        short={"logic": "AND", "conditions": [short_ema, dict(volume)]},
    )
    rules.update(overrides)
    return rules


def _insert(rows, symbol="AAPL", market="US", start=date(2024, 6, 3)):
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
            "id": "k3",
            "name": "k3",
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


def _ohlc(closes, volume=1000, spikes=None):
    spikes = spikes or {}
    rows = []
    for i, close in enumerate(closes):
        px = float(close)
        rows.append((px, px, px, px, spikes.get(i, volume)))
    return rows


def _ramp(n=30):
    return [float(100 + i) for i in range(n)]


def _fall(n=30):
    return [float(200 - i) for i in range(n)]


def _flat_rise_crash():
    """Flat 100s, then 101..110, then a 6-point slide.

    EMA(9) crosses below EMA(21) on bar 37. SMA(9) is still above SMA(21)
    on that bar and does not cross until bar 39, so a fill on bar 38 is the
    EMA path.
    """
    closes = [100.0] * 25
    closes.extend(float(100 + i) for i in range(1, 11))
    closes.extend(float(110 - 6 * i) for i in range(1, 16))
    assert len(closes) == 50
    assert closes[25] == 101 and closes[26] == 102
    assert closes[37] == 92 and closes[38] == 86 and closes[40] == 74
    return closes


def test_ema_recursive_seed_matches_adjust_false():
    """pandas ewm(adjust=False) seeds y0 = x0 and hides it until min_periods."""
    got = ema(pd.Series([1.0, 2.0, 3.0]), 2)
    assert pd.isna(got.iloc[0])
    assert got.iloc[1] == pytest.approx(5 / 3)
    assert got.iloc[2] == pytest.approx((1 / 3) * (5 / 3) + (2 / 3) * 3)

    ramp = pd.Series(_ramp(21))
    assert ema(ramp, 9).iloc[20] == pytest.approx(116.04611686018431)
    assert ema(ramp, 21).iloc[20] == pytest.approx(111.48643628024142)
    assert EMA_FAST_DEFAULT == 9
    assert EMA_SLOW_DEFAULT == 21


def test_omitted_ema_knobs_default_and_reject_the_sma_menu_and_equal_spans():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert signal_requirements(validate_rules(legacy))["ema_spans"] == []

    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "ema_gt_ema"}]},
            short={"logic": "AND", "conditions": [{"type": "ema_lt_ema"}]},
        )
    )
    assert rules["long"]["conditions"][0] == {
        "type": "ema_gt_ema",
        "fast": EMA_FAST_DEFAULT,
        "slow": EMA_SLOW_DEFAULT,
    }
    assert rules["short"]["conditions"][0] == {
        "type": "ema_lt_ema",
        "fast": EMA_FAST_DEFAULT,
        "slow": EMA_SLOW_DEFAULT,
    }
    assert "trail_pct" not in rules
    assert windows_used(rules) == []
    assert signal_requirements(rules)["ema_spans"] == [9, 21]

    swapped = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [{"type": "ema_gt_ema", "fast": 21.0, "slow": 9}],
            }
        )
    )
    assert swapped["long"]["conditions"][0] == {"type": "ema_gt_ema", "fast": 21, "slow": 9}

    mixed = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 20},
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                    {"type": "rel_volume", "k": 1.5, "window": 20},
                    {"type": "ema_gt_ema"},
                    {"type": "ema_gt_ema", "fast": 21, "slow": 50},
                ],
            }
        )
    )
    assert windows_used(mixed) == [20]
    reqs = signal_requirements(mixed)
    assert reqs["roc_periods"] == [7]
    assert reqs["volume_windows"] == [20]
    assert reqs["ema_spans"] == [9, 21, 50]

    with pytest.raises(ValueError, match="not in the fixed menu"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 21}]})
        )
    with pytest.raises(ValueError, match="different spans"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "fast": 9, "slow": 9}]})
        )
    with pytest.raises(ValueError, match="ema_gt_ema fast"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "fast": 0}]})
        )
    with pytest.raises(ValueError, match="ema_lt_ema slow"):
        validate_rules(
            _rules(short={"logic": "AND", "conditions": [{"type": "ema_lt_ema", "slow": 501}]})
        )
    with pytest.raises(ValueError, match="ema_gt_ema fast"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "fast": True}]})
        )
    with pytest.raises(ValueError, match="ema_gt_ema slow"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "slow": 9.5}]})
        )


def test_stack_comparison_is_strict_and_warmup_is_false():
    bull = SignalView(close=120, mas={}, mas_prev={}, emas={9: 116.0, 21: 111.0})
    bear = SignalView(close=180, mas={}, mas_prev={}, emas={9: 184.0, 21: 188.0})
    tied = SignalView(close=100, mas={}, mas_prev={}, emas={9: 100.0, 21: 100.0})
    cold = SignalView(close=120, mas={}, mas_prev={}, emas={9: 116.0})
    gt = {"type": "ema_gt_ema", "fast": 9, "slow": 21}
    lt = {"type": "ema_lt_ema", "fast": 9, "slow": 21}
    assert condition_true(gt, bull) is True
    assert condition_true(lt, bull) is False
    assert condition_true(gt, bear) is False
    assert condition_true(lt, bear) is True
    assert condition_true(gt, tied) is False
    assert condition_true(lt, tied) is False
    assert condition_true(gt, cold) is False
    # fast/slow are the two spans. 21 > 9 on a bullish pair is false.
    assert condition_true({"type": "ema_gt_ema", "fast": 21, "slow": 9}, bull) is False

    rules = validate_rules(_k3())
    quiet = SignalView(
        close=120, mas={}, mas_prev={}, volume=1000, vol_sma={20: 1000.0}, emas={9: 116.0, 21: 111.0}
    )
    loud = SignalView(
        close=120, mas={}, mas_prev={}, volume=2000, vol_sma={20: 1050.0}, emas={9: 116.0, 21: 111.0}
    )
    loud_bear = SignalView(
        close=180, mas={}, mas_prev={}, volume=2000, vol_sma={20: 1050.0}, emas={9: 111.0, 21: 116.0}
    )
    assert target_side(rules, quiet, "flat") == "flat"
    assert target_side(rules, quiet, "long") == "long"
    assert target_side(rules, loud, "flat") == "long"
    assert target_side(rules, loud_bear, "long") == "short"
    # k=1 treats volume equal to the average as a confirm. k=1.5 does not.
    at_average = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema"},
                    {"type": "rel_volume", "k": 1, "window": 20},
                ],
            }
        )
    )
    assert target_side(at_average, quiet, "flat") == "long"


def test_volume_gates_the_9_21_long_and_short_entries():
    start = date(2024, 6, 3)
    con = _insert(_ohlc(_ramp(), spikes={20: 2000}), start=start)
    try:
        end = start + timedelta(days=29)
        entered = _run(con, _k3(), start, end)
        assert entered["metrics"]["trade_count"] == 0
        assert entered["open_position"]["side"] == "long"
        assert entered["open_position"]["entry_date"] == _day(start, 21)
        assert entered["open_position"]["entry_price"] == 121
    finally:
        con.close()

    con = _insert(_ohlc(_ramp()), symbol="MSFT", start=start)
    try:
        end = start + timedelta(days=29)
        quiet = _run(con, _k3(), start, end, symbol="MSFT")
        assert quiet["metrics"]["trade_count"] == 0
        assert quiet["open_position"] is None

        averaged = _k3()
        averaged["long"]["conditions"][1]["k"] = 1
        at_sma = _run(con, averaged, start, end, symbol="MSFT")
        assert at_sma["open_position"]["entry_date"] == _day(start, 21)
        assert at_sma["open_position"]["entry_price"] == 121
    finally:
        con.close()

    con = _insert(_ohlc(_fall(), spikes={20: 2000}), symbol="QQQ", start=start)
    try:
        end = start + timedelta(days=29)
        shorted = _run(con, _k3(), start, end, symbol="QQQ")
        assert shorted["open_position"]["side"] == "short"
        assert shorted["open_position"]["entry_date"] == _day(start, 21)
        assert shorted["open_position"]["entry_price"] == 179
    finally:
        con.close()


def test_equal_emas_do_not_fire_even_when_volume_spikes():
    start = date(2024, 7, 1)
    closes = [100.0] * 30
    con = _insert(_ohlc(closes, spikes={25: 5000}), start=start)
    try:
        result = _run(con, _k3(), start, start + timedelta(days=29))
        assert result["metrics"]["trade_count"] == 0
        assert result["open_position"] is None
    finally:
        con.close()


def test_ema_uses_bars_before_the_backtest_window():
    """Eight in-range bars cannot warm EMA(21). The seed is the earlier history."""
    start = date(2024, 8, 1)
    con = _insert(_ohlc(_ramp(), spikes={22: 2000}), start=start)
    try:
        window = start + timedelta(days=22)
        end = start + timedelta(days=29)
        result = _run(con, _k3(), window, end)
        assert result["open_position"]["side"] == "long"
        assert result["open_position"]["entry_date"] == _day(start, 23)
        assert result["open_position"]["entry_price"] == 123
    finally:
        con.close()


def test_ema_stack_flips_short_before_sma_9_21_would():
    start = date(2024, 9, 2)
    closes = _flat_rise_crash()
    con = _insert(_ohlc(closes), start=start)
    try:
        rules = _rules(
            long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "fast": 9, "slow": 21}]},
            short={"logic": "AND", "conditions": [{"type": "ema_lt_ema", "fast": 9, "slow": 21}]},
        )
        result = _run(con, rules, start, start + timedelta(days=len(closes) - 1))
        assert result["metrics"]["trade_count"] == 1
        trade = result["trades"][0]
        assert trade["side"] == "long"
        assert trade["entry_date"] == _day(start, 26)
        assert trade["entry_price"] == 102
        assert trade["exit_date"] == _day(start, 38)
        assert trade["exit_price"] == 86
        assert trade["exit_reason"] == "signal"
        assert result["open_position"]["side"] == "short"
        assert result["open_position"]["entry_price"] == 86
        shares = 0.20 * 100_000 / 102
        assert trade["shares"] == round(shares, 8)
        assert trade["pnl"] == round((86 - 102) * shares, 6)
    finally:
        con.close()


def test_quiet_volume_holds_through_a_stack_flip_until_volume_confirms():
    start = date(2024, 10, 1)
    closes = _flat_rise_crash()
    end = start + timedelta(days=len(closes) - 1)
    con = _insert(_ohlc(closes, spikes={25: 2000}), start=start)
    try:
        held = _run(con, _k3(), start, end)
        assert held["metrics"]["trade_count"] == 0
        assert held["open_position"]["side"] == "long"
        assert held["open_position"]["entry_date"] == _day(start, 26)
        assert held["open_position"]["entry_price"] == 102
    finally:
        con.close()

    con = _insert(_ohlc(closes, spikes={25: 2000, 37: 2000}), symbol="MSFT", start=start)
    try:
        flipped = _run(con, _k3(), start, end, symbol="MSFT")
        assert flipped["metrics"]["trade_count"] == 1
        trade = flipped["trades"][0]
        assert trade["entry_date"] == _day(start, 26)
        assert trade["exit_date"] == _day(start, 38)
        assert trade["exit_price"] == 86
        assert trade["exit_reason"] == "signal"
        assert flipped["open_position"]["side"] == "short"
        assert flipped["open_position"]["entry_date"] == _day(start, 38)
    finally:
        con.close()


def test_ema_ands_with_roc_rsi_donchian_and_ma():
    start = date(2024, 11, 1)
    # Half a percent is not ROC(7) ≥ +1, but it does lift EMA(9) above EMA(21).
    closes = [100.0] * 25 + [100.5, 100.5, 100.5]
    con = _insert(_ohlc(closes, spikes={25: 5000}), start=start)
    try:
        end = start + timedelta(days=len(closes) - 1)
        blocked = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema", "fast": 9, "slow": 21},
                    {"type": "rel_volume", "k": 1.5, "window": 20},
                    {"type": "roc_gte", "period": 7, "threshold": 1},
                ],
            }
        )
        assert _run(con, blocked, start, end)["open_position"] is None

        opened = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema", "fast": 9, "slow": 21},
                    {"type": "rel_volume", "k": 1.5, "window": 20},
                ],
            }
        )
        lone = _run(con, opened, start, end)
        assert lone["open_position"]["entry_date"] == _day(start, 26)
        assert lone["open_position"]["entry_price"] == 100.5
    finally:
        con.close()

    ramp_start = date(2024, 11, 15)
    con = _insert(_ohlc(_ramp(), spikes={20: 2000}), symbol="MSFT", start=ramp_start)
    try:
        end = ramp_start + timedelta(days=29)
        together = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema"},
                    {"type": "rel_volume"},
                    {"type": "roc_gte"},
                    {"type": "price_gt_ma", "window": 20},
                    {"type": "donchian_high", "window": 20},
                ],
            }
        )
        hit = _run(con, together, ramp_start, end, symbol="MSFT")
        assert hit["open_position"]["entry_date"] == _day(ramp_start, 21)
        assert hit["open_position"]["entry_price"] == 121

        rsi_block = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema"},
                    {"type": "rel_volume"},
                    {"type": "rsi_lte", "period": 2, "threshold": 5},
                ],
            }
        )
        assert _run(con, rsi_block, ramp_start, end, symbol="MSFT")["open_position"] is None

        roc_block = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema"},
                    {"type": "roc_gte", "period": 7, "threshold": 50},
                ],
            }
        )
        assert _run(con, roc_block, ramp_start, end, symbol="MSFT")["open_position"] is None
    finally:
        con.close()


def test_trail_still_exits_an_ema_long_at_the_next_open():
    start = date(2024, 12, 2)
    rows = _ohlc(_ramp())
    rows[20] = (120, 120, 120, 120, 2000)
    rows[22] = (121, 121, 110, 110, 1000)
    rows[23] = (109, 109, 109, 109, 1000)
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        rules = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "ema_gt_ema", "fast": 9, "slow": 21},
                    {"type": "rel_volume", "k": 1.5, "window": 20},
                ],
            },
            trail_pct=5,
        )
        stopped = _run(con, rules, start, end)
        assert stopped["metrics"]["trade_count"] == 1
        trade = stopped["trades"][0]
        assert trade["side"] == "long"
        assert trade["entry_date"] == _day(start, 21)
        assert trade["entry_price"] == 121
        assert trade["exit_date"] == _day(start, 23)
        assert trade["exit_price"] == 109
        assert trade["exit_reason"] == "trail"
        assert stopped["open_position"] is None
        shares = 0.20 * 100_000 / 121
        assert trade["pnl"] == round((109 - 121) * shares, 6)
    finally:
        con.close()


def test_default_costs_are_unchanged_on_an_ema_backtest():
    start = date(2025, 1, 6)
    rows = _ohlc(_ramp())
    rows[20] = (120, 120, 120, 120, 2000)
    rows[22] = (121, 121, 110, 110, 1000)
    rows[23] = (109, 109, 109, 109, 1000)
    con = _insert(rows, start=start)
    try:
        result = _run(
            con,
            _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "ema_gt_ema"},
                        {"type": "rel_volume"},
                    ],
                },
                trail_pct=5,
            ),
            start,
            start + timedelta(days=len(rows) - 1),
            costs={},
        )
        assert result["costs"] == CostModel().to_dict()
        assert result["starting_capital"] == 100_000
        assert result["currency"] == "USD"
        assert result["trades"][0]["exit_reason"] == "trail"
        assert result["trades"][0]["entry_commission"] >= 1
        assert result["trades"][0]["exit_commission"] >= 1
    finally:
        con.close()

    con = _insert([(100, 100, 100, 100, 1000)], symbol="0700.HK", market="HK", start=start)
    try:
        hk = _run(con, _k3(), start, start, costs={}, symbol="0700.HK", market="HK")
        assert hk["costs"] == CostModel().to_dict()
        assert hk["starting_capital"] == 500_000
        assert hk["currency"] == "HKD"
        assert hk["metrics"]["trade_count"] == 0
    finally:
        con.close()


def test_api_persists_k3_rules_and_rejects_equal_spans():
    start = date(2025, 2, 3)
    rows = _ohlc(_ramp(), spikes={20: 2000})
    con = _insert(rows, start=start)
    con.close()
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "K3 EMA",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "ema_gt_ema"}, {"type": "rel_volume"}]},
                short={"logic": "AND", "conditions": [{"type": "ema_lt_ema"}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [
        {"type": "ema_gt_ema", "fast": 9, "slow": 21},
        {"type": "rel_volume", "k": 1.5, "window": 20},
    ]
    assert body["rules"]["short"]["conditions"] == [
        {"type": "ema_lt_ema", "fast": 9, "slow": 21},
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
    assert run.json()["open_position"]["entry_date"] == _day(start, 21)
    assert run.json()["open_position"]["entry_price"] == 121

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad ema",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "ema_gt_ema", "fast": 9, "slow": 9}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_research_panel_can_author_a_k3_strategy():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert 'id: "ema"' in script
    assert "EMA 9/21 + volume" in script
    assert "EMA(9) is above EMA(21)" in script
    assert "1.5× its 20-bar average" in script
    assert "ema_gt_ema: { fast: 9, slow: 21 }" in script
    assert "ema_lt_ema: { fast: 9, slow: 21 }" in script
    assert "function isEmaType" in script
    assert "cond-slow" in script
    assert "EMA fast span" in script
    assert "defaults 9 and 21" in html
    assert "k=1.5 and N=20" in html

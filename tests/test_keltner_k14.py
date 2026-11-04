"""Keltner mean reversion on the next-open book (S1-K14).

Public pattern: long when the close is below the lower band, flat when the
close is above the mid. The mid is the EMA of typical price (H+L+C)/3.
The bands are that mid ± Wilder ATR × multiplier. Public defaults are
length 6 and multiplier 1.3. Long only, no trail.

Wilder ATR here is the SMA of the first `n` true ranges, then
ATR = ((n-1)*ATR + TR) / n. That is not the RSI `ewm(adjust=False)` seed.

The channel is not Bollinger (close ± standard deviation) and not Donchian
(prior high / prior low). The overnight grid stays `s1-board-v1` at 25 cells.
Signal cases use zero costs. The golden reruns the same AAPL tape on US v1.2.
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
from dixon.compute_indicators import (
    atr,
    bollinger,
    ema,
    keltner_channels,
    keltner_mid,
    prior_low,
    true_range,
    typical_price,
)
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import (
    KELTNER_ATR_MULT_DEFAULT,
    KELTNER_LEN_DEFAULT,
    SignalView,
    condition_true,
    signal_requirements,
    target_side,
    validate_rules,
    windows_used,
)
from dixon.strategy_board import GRID_VERSION, grid_cells

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dixon" / "static"
FIXTURE = Path(__file__).parent / "fixtures" / "keltner_n6_m13_aapl.json"
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


def _public(length=6, mult=1.3):
    """Public book. Same length and multiplier on the entry and the exit."""
    return _rules(
        long={
            "logic": "AND",
            "conditions": [
                {"type": "keltner_below", "keltner_len": length, "atr_mult": mult}
            ],
        },
        flat={
            "logic": "OR",
            "conditions": [
                {"type": "keltner_above", "keltner_len": length, "atr_mult": mult}
            ],
        },
    )


def _insert(rows, symbol="AAPL", market="US", start=GOLDEN_START):
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
            "id": "k14",
            "name": "keltner",
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


def _with_volume(rows, volume=1_000_000):
    return [(*bar, volume) for bar in rows]


def public_tape():
    """Calm 100s, one close under the 6/1.3 lower band, then a reclaim of the mid.

    Index 16 is the only `keltner_below` bar (next open is the entry).
    Indexes 17 and 18 sit between the lower band and the mid, so the long is
    held. Index 19 is the first close above the mid (next open is the exit).
    Length 10 / multiplier 2.0 never prints a below on this tape.
    """
    rows = []
    prev = 100.0
    span = 0.30
    for _ in range(16):
        close = 100.0
        rows.append((prev, max(prev, close) + span, min(prev, close) - span, close))
        prev = close
    rows.append((100.0, 100.05, 98.75, 98.80))
    rows.append((98.80, 99.10, 98.70, 99.00))
    rows.append((99.00, 99.30, 98.90, 99.20))
    rows.append((99.20, 100.50, 99.10, 100.40))
    rows.append((100.40, 100.60, 100.10, 100.30))
    rows.append((100.30, 100.55, 100.05, 100.25))
    assert len(rows) == 22
    return rows


def _frame(rows):
    return (
        pd.Series([r[1] for r in rows], dtype=float),
        pd.Series([r[2] for r in rows], dtype=float),
        pd.Series([r[3] for r in rows], dtype=float),
    )


def _flags(rows, length, mult):
    high, low, close = _frame(rows)
    mid, _upper, lower, _width = keltner_channels(high, low, close, length, mult)
    below = []
    above = []
    for i in range(len(rows)):
        if pd.notna(lower.iloc[i]) and close.iloc[i] < lower.iloc[i]:
            below.append(i)
        if pd.notna(mid.iloc[i]) and close.iloc[i] > mid.iloc[i]:
            above.append(i)
    return below, above


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


def test_wilder_atr_seeds_with_the_sma_and_is_not_the_rsi_ewm():
    """First ATR is the mean of the first n true ranges. Later bars are the RMA.

    True ranges 1, 2, 3, 6 and period 3:
      index 2 = (1+2+3)/3 = 2
      index 3 = (2*2 + 6)/3 = 10/3
    `ewm(alpha=1/3, adjust=False)` seeds on the first true range and prints
    17/9 then 88/27. RSI uses that seed. ATR does not.
    """
    trs = [1.0, 2.0, 3.0, 6.0]
    close = pd.Series([100.0] * 4)
    high = pd.Series([100.0 + tr / 2.0 for tr in trs])
    low = pd.Series([100.0 - tr / 2.0 for tr in trs])
    got_tr = true_range(high, low, close)
    assert got_tr.tolist() == pytest.approx(trs)
    got = atr(high, low, close, 3)
    assert got.iloc[:2].isna().all()
    assert got.iloc[2] == pytest.approx(2.0)
    assert got.iloc[3] == pytest.approx(10.0 / 3.0)
    ewm = got_tr.ewm(alpha=1.0 / 3.0, adjust=False, min_periods=3).mean()
    assert ewm.iloc[2] == pytest.approx(17.0 / 9.0)
    assert got.iloc[2] != pytest.approx(ewm.iloc[2])
    assert got.iloc[3] != pytest.approx(ewm.iloc[3])
    # A constant true range hides the seed. The hand channel below uses that.
    flat = true_range(pd.Series([10.0, 11, 12, 14]), pd.Series([8.0, 9, 10, 10]), pd.Series([9.0, 10, 11, 12]))
    assert flat.tolist() == pytest.approx([2.0, 2.0, 2.0, 4.0])


def test_keltner_band_math_ema_typical_plus_wilder_atr():
    """Period 3, multiplier 1.3. EMA span 3 has alpha 1/2, so the mid is exact.

    Typical prices 9, 10, 11, 12.
    EMA: y0=9, y1=9.5, y2=10.25, y3=11.125. min_periods hides the first two.
    ATR: 2, then 8/3. Lower = mid − 1.3×ATR. Upper = mid + 1.3×ATR.
    """
    high = pd.Series([10.0, 11.0, 12.0, 14.0])
    low = pd.Series([8.0, 9.0, 10.0, 10.0])
    close = pd.Series([9.0, 10.0, 11.0, 12.0])
    typical = typical_price(high, low, close)
    assert typical.tolist() == pytest.approx([9.0, 10.0, 11.0, 12.0])
    assert keltner_mid(high, low, close, 3).tolist()[2:] == pytest.approx(
        ema(typical, 3).tolist()[2:]
    )
    mid, upper, lower, width = keltner_channels(high, low, close, 3, 1.3)
    assert mid.iloc[:2].isna().all()
    assert width.iloc[:2].isna().all()
    assert lower.iloc[:2].isna().all()
    assert mid.iloc[2] == pytest.approx(10.25)
    assert mid.iloc[3] == pytest.approx(11.125)
    assert width.iloc[2] == pytest.approx(2.0)
    assert width.iloc[3] == pytest.approx(8.0 / 3.0)
    assert lower.iloc[2] == pytest.approx(10.25 - 1.3 * 2.0)
    assert upper.iloc[2] == pytest.approx(10.25 + 1.3 * 2.0)
    assert lower.iloc[3] == pytest.approx(11.125 - 1.3 * (8.0 / 3.0))
    assert upper.iloc[3] == pytest.approx(11.125 + 1.3 * (8.0 / 3.0))
    assert KELTNER_LEN_DEFAULT == 6
    assert KELTNER_ATR_MULT_DEFAULT == 1.3


def test_length_one_is_typical_price_plus_or_minus_true_range():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([8.0, 11.0])
    close = pd.Series([9.0, 11.5])
    mid, upper, lower, width = keltner_channels(high, low, close, 1, 1.3)
    assert width.tolist() == pytest.approx([2.0, 3.0])
    assert mid.tolist() == pytest.approx(typical_price(high, low, close).tolist())
    assert lower.iloc[0] == pytest.approx(mid.iloc[0] - 1.3 * 2.0)
    assert upper.iloc[1] == pytest.approx(mid.iloc[1] + 1.3 * 3.0)


def test_warmup_is_missing_until_both_ema_and_atr_exist():
    high, low, close = _frame(public_tape())
    mid, upper, lower, width = keltner_channels(high, low, close, 6, 1.3)
    assert mid.iloc[:5].isna().all()
    assert lower.iloc[:5].isna().all()
    assert upper.iloc[:5].isna().all()
    assert width.iloc[:5].isna().all()
    assert pd.notna(mid.iloc[5]) and pd.notna(lower.iloc[5])
    # Five bars cannot finish length 6. The book stays flat.
    con = _insert(_with_volume(public_tape()[:5]))
    try:
        result = _run(con, _public(), GOLDEN_START, GOLDEN_START + timedelta(days=4))
    finally:
        con.close()
    assert result["trades"] == []
    assert result["open_position"] is None


def test_bands_are_not_bollinger_and_not_donchian():
    """Same length and multiplier still disagree with a standard-deviation band.

    Flat closes and a 10-point range: Bollinger width collapses, ATR does not.
    A separate path shows each directional disagreement with Donchian.
    """
    high = pd.Series([10.0, 11.0, 12.0, 14.0])
    low = pd.Series([8.0, 9.0, 10.0, 10.0])
    close = pd.Series([9.0, 10.0, 11.0, 12.0])
    mid, _upper, lower, _width = keltner_channels(high, low, close, 3, 1.3)
    bb_mid, _bb_up, bb_low = bollinger(close, 3, 1.3)
    assert bb_mid.iloc[2] == pytest.approx(10.0)
    assert mid.iloc[2] == pytest.approx(10.25)
    assert mid.iloc[2] != pytest.approx(bb_mid.iloc[2])
    assert lower.iloc[2] != pytest.approx(bb_low.iloc[2])

    wide_high = pd.Series([55.0] * 8)
    wide_low = pd.Series([45.0] * 8)
    wide_close = pd.Series([50.0] * 8)
    k_mid, _k_up, k_low, k_atr = keltner_channels(wide_high, wide_low, wide_close, 6, 1.3)
    b_mid, _b_up, b_low = bollinger(wide_close, 6, 1.3)
    assert k_atr.iloc[6] == pytest.approx(10.0)
    assert k_low.iloc[6] == pytest.approx(37.0)
    assert b_low.iloc[6] == pytest.approx(50.0)
    assert k_mid.iloc[6] == pytest.approx(b_mid.iloc[6])
    assert k_low.iloc[6] != pytest.approx(b_low.iloc[6])
    assert prior_low(wide_low, 6).iloc[6] == pytest.approx(45.0)
    assert k_low.iloc[6] != pytest.approx(prior_low(wide_low, 6).iloc[6])

    # Close under the Keltner lower, still above the prior 6-bar low (a wick).
    under = []
    under.extend((110.0, 110.05, 109.95, 110.0) for _ in range(6))
    under.append((110.0, 110.05, 108.0, 110.0))
    under.extend((110.0, 110.05, 109.95, 110.0) for _ in range(2))
    under.append((110.0, 110.02, 109.18, 109.2))
    u_high, u_low, u_close = _frame(under)
    _u_mid, _u_up, u_lower, _u_atr = keltner_channels(u_high, u_low, u_close, 6, 1.3)
    u_prior = prior_low(u_low, 6)
    assert u_close.iloc[-1] < u_lower.iloc[-1]
    assert not (u_close.iloc[-1] < u_prior.iloc[-1])

    # Close through the prior low, still above a wide ATR band.
    through = [(100.0, 102.0, 98.0, 100.0) for _ in range(10)]
    through.append((100.0, 100.0, 97.2, 97.5))
    t_high, t_low, t_close = _frame(through)
    _t_mid, _t_up, t_lower, _t_atr = keltner_channels(t_high, t_low, t_close, 6, 1.3)
    t_prior = prior_low(t_low, 6)
    assert t_close.iloc[-1] < t_prior.iloc[-1]
    assert not (t_close.iloc[-1] < t_lower.iloc[-1])


def test_strict_compare_warmup_and_the_mid_not_the_upper_band():
    below = {"type": "keltner_below", "keltner_len": 6, "atr_mult": 1.3}
    above = {"type": "keltner_above", "keltner_len": 6, "atr_mult": 1.3}
    view = SignalView(
        close=11.0,
        mas={},
        mas_prev={},
        keltner_lower={(6, 1.3): 10.0},
        keltner_mid={(6, 1.3): 12.0},
    )
    # 11 is between the lower band and the mid, and below a hypothetical upper.
    assert condition_true(below, view) is False
    assert condition_true(above, view) is False
    on_lower = SignalView(close=10.0, mas={}, mas_prev={}, keltner_lower={(6, 1.3): 10.0}, keltner_mid={(6, 1.3): 12.0})
    on_mid = SignalView(close=12.0, mas={}, mas_prev={}, keltner_lower={(6, 1.3): 10.0}, keltner_mid={(6, 1.3): 12.0})
    assert condition_true(below, on_lower) is False
    assert condition_true(above, on_mid) is False
    under = SignalView(close=9.5, mas={}, mas_prev={}, keltner_lower={(6, 1.3): 10.0}, keltner_mid={(6, 1.3): 12.0})
    over = SignalView(close=12.5, mas={}, mas_prev={}, keltner_lower={(6, 1.3): 10.0}, keltner_mid={(6, 1.3): 12.0})
    assert condition_true(below, under) is True
    assert condition_true(above, under) is False
    assert condition_true(above, over) is True
    assert condition_true(below, over) is False
    cold = SignalView(close=9.5, mas={}, mas_prev={})
    assert condition_true(below, cold) is False
    assert condition_true(above, cold) is False
    other = {"type": "keltner_below", "keltner_len": 10, "atr_mult": 2.0}
    assert condition_true(other, under) is False

    rules = validate_rules(_public())
    assert target_side(rules, under, "flat") == "long"
    assert target_side(rules, view, "long") == "long"
    assert target_side(rules, view, "flat") == "flat"
    assert target_side(rules, over, "long") == "flat"
    assert target_side(rules, cold, "long") == "long"
    assert target_side(rules, on_lower, "flat") == "flat"
    assert target_side(rules, on_mid, "long") == "long"


def test_omitted_knobs_default_to_public_6_and_1_3():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    assert signal_requirements(validate_rules(legacy))["keltner"] == []
    assert "trail_pct" not in validate_rules(legacy)

    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "keltner_below"}]},
            flat={"logic": "OR", "conditions": [{"type": "keltner_above"}]},
        )
    )
    assert rules["long"]["conditions"] == [
        {"type": "keltner_below", "keltner_len": 6, "atr_mult": 1.3}
    ]
    assert rules["flat"]["conditions"] == [
        {"type": "keltner_above", "keltner_len": 6, "atr_mult": 1.3}
    ]
    assert rules["short"]["conditions"] == []
    assert "trail_pct" not in rules
    assert windows_used(rules) == []
    assert signal_requirements(rules)["keltner"] == [[6, 1.3]]
    assert signal_requirements(rules)["ema_spans"] == []

    swept = validate_rules(_public(length=10, mult=2))
    assert signal_requirements(swept)["keltner"] == [[10, 2.0]]
    both = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "keltner_below", "keltner_len": 6, "atr_mult": 1.3},
                    {"type": "keltner_below", "keltner_len": 10, "atr_mult": 2.0},
                ],
            }
        )
    )
    assert signal_requirements(both)["keltner"] == [[6, 1.3], [10, 2.0]]

    with pytest.raises(ValueError, match="keltner_len"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "keltner_below", "keltner_len": 0}]})
        )
    with pytest.raises(ValueError, match="keltner_len"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "keltner_below", "keltner_len": 501}]})
        )
    with pytest.raises(ValueError, match="keltner_len"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "keltner_below", "keltner_len": True}]})
        )
    with pytest.raises(ValueError, match="keltner_len"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "keltner_below", "keltner_len": 6.5}]})
        )
    with pytest.raises(ValueError, match="atr_mult"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "keltner_above", "atr_mult": 0}]})
        )
    with pytest.raises(ValueError, match="atr_mult"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "keltner_above", "atr_mult": -1}]})
        )
    with pytest.raises(ValueError, match="atr_mult"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "keltner_above", "atr_mult": 100.01}]})
        )
    with pytest.raises(ValueError, match="atr_mult"):
        validate_rules(
            _rules(flat={"logic": "OR", "conditions": [{"type": "keltner_above", "atr_mult": "1.3"}]})
        )


def test_public_tape_flags_and_the_secondary_knobs_disagree():
    below, above = _flags(public_tape(), 6, 1.3)
    assert below == [16]
    assert 19 in above and 17 not in above and 18 not in above
    wide_below, _wide_above = _flags(public_tape(), 10, 2.0)
    assert wide_below == []


def test_next_open_entry_hold_and_exit_on_the_public_book():
    start = GOLDEN_START
    rows = _with_volume(public_tape())
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _public(), start, end)
    finally:
        con.close()
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "long"
    assert trade["entry_date"] == _day(start, 17)
    assert trade["exit_date"] == _day(start, 20)
    assert trade["entry_price"] == pytest.approx(98.80)
    assert trade["exit_price"] == pytest.approx(100.40)
    assert trade["exit_reason"] == "signal"
    assert result["open_position"] is None
    sides = {point["date"]: point["side"] for point in result["equity_curve"]}
    assert sides[_day(start, 16)] == "flat"
    assert sides[_day(start, 17)] == "long"
    assert sides[_day(start, 18)] == "long"
    assert sides[_day(start, 19)] == "long"
    assert sides[_day(start, 20)] == "flat"

    # The signal on the last stored bar has no next open.
    con = _insert(_with_volume(public_tape()[:17]), symbol="MSFT", start=start)
    try:
        hung = _run(con, _public(), start, start + timedelta(days=16), symbol="MSFT")
    finally:
        con.close()
    assert hung["trades"] == []
    assert hung["open_position"] is None

    # Bars before the window still seed the EMA and the ATR. The 10/2 book
    # never goes below its lower band on this tape, so the knobs are live.
    con = _insert(rows, symbol="SPY", start=start)
    try:
        warmed = _run(
            con, _public(), start + timedelta(days=16), end, symbol="SPY"
        )
        secondary = _run(con, _public(length=10, mult=2.0), start, end, symbol="SPY")
    finally:
        con.close()
    assert warmed["trades"][0]["entry_date"] == _day(start, 17)
    assert warmed["trades"][0]["exit_date"] == _day(start, 20)
    assert secondary["trades"] == []
    assert secondary["open_position"] is None


def test_golden_aapl_keltner_is_bit_stable_on_the_default_us_book():
    """Default US costs. One closed long. The fixture is the whole marked book."""
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    start = GOLDEN_START
    rows = _with_volume(public_tape())
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _public(), start, end, costs={})
    finally:
        con.close()

    assert result["costs"] == CostModel().to_dict()
    assert result["currency"] == "USD"
    assert result["starting_capital"] == 100_000
    assert result["metrics"]["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][0]["exit_reason"] == "signal"
    assert result["open_position"] is None
    assert result["metrics"]["total_pnl"] == pytest.approx(result["trades"][0]["pnl"])
    assert _roundtrip(_locked(result)) == expected["result"]
    assert expected["symbol"] == "AAPL"
    assert expected["keltner_len"] == 6
    assert expected["atr_mult"] == 1.3
    assert expected["costs"] == "us-v1.2-defaults"
    assert expected["note"] == "ATR envelope, not Bollinger and not Donchian"


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/keltner_n6_m13_aapl.json. Not used by pytest."""
    start = GOLDEN_START
    rows = _with_volume(public_tape())
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        result = _run(con, _public(), start, end, costs={})
    finally:
        con.close()
    payload = {
        "symbol": "AAPL",
        "keltner_len": 6,
        "atr_mult": 1.3,
        "book": "long keltner_below / flat keltner_above",
        "costs": "us-v1.2-defaults",
        "note": "ATR envelope, not Bollinger and not Donchian",
        "atr": "wilder-sma-seed",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "closed_pnl": result["trades"][0]["pnl"],
        "result": _roundtrip(_locked(result)),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def test_api_persists_the_public_defaults_and_rejects_a_bad_multiplier():
    start = date(2024, 6, 3)
    rows = _with_volume(public_tape())
    _insert(rows, start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "Keltner MR",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "keltner_below"}]},
                flat={"logic": "OR", "conditions": [{"type": "keltner_above"}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"] == [
        {"type": "keltner_below", "keltner_len": 6, "atr_mult": 1.3}
    ]
    assert body["rules"]["flat"]["conditions"] == [
        {"type": "keltner_above", "keltner_len": 6, "atr_mult": 1.3}
    ]
    assert body["rules"]["short"]["conditions"] == []
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
    assert run.json()["trades"][0]["entry_date"] == _day(start, 17)
    assert run.json()["trades"][0]["exit_reason"] == "signal"

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad mult",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "keltner_below", "atr_mult": 0}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_default_costs_are_unchanged_on_a_keltner_backtest():
    start = date(2024, 7, 1)
    rows = _with_volume(public_tape(), volume=1000)
    con = _insert(rows, start=start)
    try:
        result = _run(con, _public(), start, start + timedelta(days=len(rows) - 1), costs={})
    finally:
        con.close()
    assert result["costs"] == CostModel().to_dict()
    assert result["starting_capital"] == 100_000
    assert result["currency"] == "USD"
    assert result["trades"][0]["entry_commission"] >= 1
    assert result["trades"][0]["exit_commission"] >= 1

    con = _insert([(100, 100, 100, 100, 1000)], symbol="0700.HK", market="HK", start=start)
    try:
        hk = _run(con, _public(), start, start, costs={}, symbol="0700.HK", market="HK")
    finally:
        con.close()
    assert hk["costs"] == CostModel().to_dict()
    assert hk["starting_capital"] == 500_000
    assert hk["currency"] == "HKD"
    assert hk["metrics"]["trade_count"] == 0


def test_overnight_board_stays_25_cells_without_a_keltner_rule():
    cells = grid_cells()
    assert GRID_VERSION == "s1-board-v1"
    assert len(cells) == 25
    for cell in cells:
        for side in ("long", "flat", "short"):
            for cond in cell.rules()[side]["conditions"]:
                assert cond["type"] not in {"keltner_below", "keltner_above"}


def test_research_panel_wires_the_public_6_1_3_preset():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    formulas = (ROOT / "indicators" / "FORMULAS.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert 'id: "keltner"' in script
    assert 'id: "keltner10"' in script
    assert "Keltner MR 6/1.3" in script
    assert "keltner_below: { keltner_len: 6, atr_mult: 1.3 }" in script
    assert "keltner_above: { keltner_len: 6, atr_mult: 1.3 }" in script
    assert '{ type: "keltner_below", keltner_len: 6, atr_mult: 1.3 }' in script
    assert '{ type: "keltner_above", keltner_len: 6, atr_mult: 1.3 }' in script
    assert "keltner_len: 10, atr_mult: 2.0" in script
    assert "not the Public default" in script
    assert "function isKeltnerType" in script
    assert "Close < Keltner lower" in script
    assert "Close > Keltner mid" in script
    assert "not Bollinger and not Donchian" in script
    assert "Wilder ATR(6)" in script
    assert "length 6 and multiplier 1.3" in html
    assert "not Bollinger bands and not a Donchian" in html
    assert "keltner_below" in formulas and "keltner_above" in formulas
    assert "Wilder" in formulas
    assert "Not** Bollinger" in readme or "**Not** Bollinger" in readme
    assert "s1-board-v1" in (ROOT / "PRD.md").read_text()

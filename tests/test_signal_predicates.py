"""Relative volume, RSI(period, threshold), and Donchian N/M predicates.

Each backtest is a hand-built tape. Fills are the next stored bar's open.
Costs are zero so the assertion is the signal, not the commission.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.compute_indicators import prior_high, prior_low, volume_sma
from dixon.db import connect
from dixon.rules import (
    DONCHIAN_HIGH_DEFAULT,
    DONCHIAN_LOW_DEFAULT,
    REL_VOLUME_K_DEFAULT,
    REL_VOLUME_WINDOW_DEFAULT,
    RSI_GTE_THRESHOLD_DEFAULT,
    RSI_LTE_THRESHOLD_DEFAULT,
    RSI_PERIOD_DEFAULT,
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


def _insert(rows, symbol="AAPL", start=date(2024, 1, 2)):
    """rows are (open, high, low, close, volume). Dates are consecutive sessions from `start`."""
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


def _run(con, rules, start, end, symbol="AAPL"):
    return run_backtest(
        con,
        {
            "id": "pred",
            "name": "predicates",
            "symbol": symbol,
            "market": "US",
            "timeframe": "daily",
            "rules": rules,
        },
        start,
        end,
        ZERO_COSTS,
    )


def _day(start, offset):
    return (start + timedelta(days=offset)).isoformat()


def test_legacy_ma_rules_stay_canonical_and_ignore_new_lookbacks():
    legacy = {
        "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
        "short": {"logic": "AND", "conditions": []},
    }
    assert validate_rules(legacy) == legacy
    mixed = validate_rules(
        _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 20},
                    {"type": "rel_volume", "k": 1.5, "window": 15},
                    {"type": "donchian_high", "window": 20},
                ],
            }
        )
    )
    assert windows_used(mixed) == [20]


def test_omitted_predicate_knobs_store_the_public_defaults():
    rules = validate_rules(
        _rules(
            long={"logic": "AND", "conditions": [{"type": "rel_volume"}, {"type": "rsi_lte"}]},
            flat={
                "logic": "OR",
                "conditions": [{"type": "rsi_gte"}, {"type": "donchian_low"}],
            },
            short={"logic": "AND", "conditions": [{"type": "donchian_high"}]},
        )
    )
    assert rules["long"]["conditions"][0] == {
        "type": "rel_volume",
        "k": REL_VOLUME_K_DEFAULT,
        "window": REL_VOLUME_WINDOW_DEFAULT,
    }
    assert rules["long"]["conditions"][1] == {
        "type": "rsi_lte",
        "period": RSI_PERIOD_DEFAULT,
        "threshold": RSI_LTE_THRESHOLD_DEFAULT,
    }
    assert rules["flat"]["conditions"][0]["threshold"] == RSI_GTE_THRESHOLD_DEFAULT
    assert rules["flat"]["conditions"][1]["window"] == DONCHIAN_LOW_DEFAULT
    assert rules["short"]["conditions"][0]["window"] == DONCHIAN_HIGH_DEFAULT
    with pytest.raises(ValueError, match="rel_volume k"):
        validate_rules(_rules(long={"logic": "AND", "conditions": [{"type": "rel_volume", "k": 0}]}))
    with pytest.raises(ValueError, match="threshold"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "rsi_lte", "threshold": 101}]})
        )
    with pytest.raises(ValueError, match="donchian_high"):
        validate_rules(
            _rules(long={"logic": "AND", "conditions": [{"type": "donchian_high", "window": 0}]})
        )


def test_prior_channel_excludes_the_signal_bar_and_volume_sma_includes_it():
    high = pd.Series([20.0, 10.0, 10.0, 10.0, 12.0])
    low = pd.Series([19.0, 9.0, 9.0, 9.0, 8.0])
    # Index 4, N=3: highs of bars 1..3, not the 12 printed on the signal bar.
    assert prior_high(high, 3).iloc[4] == 10
    assert pd.isna(prior_high(high, 3).iloc[2])
    # Index 4, M=2: lows of bars 2 and 3 (both 9), not the signal bar's 8.
    assert prior_low(low, 2).iloc[4] == 9
    volume = pd.Series([1000, 1000, 100, 100, 100, 250])
    assert volume_sma(volume, 3).iloc[5] == pytest.approx(150)
    assert volume_sma(volume, 5).iloc[5] == pytest.approx(310)


def test_relative_volume_gates_an_otherwise_true_breakout():
    """Close is above SMA(5) from bar 4, but volume confirms only on bar 6.

    Volumes are 100 except bar 6 at 1000.
    SMA(volume, 3) on that bar is 400, and 1000 >= 1.5 * 400.
    The same bar fails k=4 (1600) and fails N=8 (not enough history).
    """
    start = date(2024, 2, 1)
    closes = [10, 11, 12, 13, 14, 15, 16, 17]
    volumes = [100, 100, 100, 100, 100, 100, 1000, 100]
    rows = [(c, c, c, c, v) for c, v in zip(closes, volumes)]
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)
        gated = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 5},
                    {"type": "rel_volume", "k": 1.5, "window": 3},
                ],
            }
        )
        ungated = _rules(
            long={"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]}
        )
        quiet = [(c, c, c, c, 100) for c in closes]

        fired = _run(con, gated, start, end)
        assert fired["metrics"]["trade_count"] == 0
        assert fired["open_position"]["entry_date"] == _day(start, 7)
        assert fired["open_position"]["entry_price"] == 17

        plain = _run(con, ungated, start, end)
        assert plain["open_position"]["entry_date"] == _day(start, 5)

        strict_k = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 5},
                    {"type": "rel_volume", "k": 4, "window": 3},
                ],
            }
        )
        assert _run(con, strict_k, start, end)["open_position"] is None

        long_window = _rules(
            long={
                "logic": "AND",
                "conditions": [
                    {"type": "price_gt_ma", "window": 5},
                    {"type": "rel_volume", "k": 1.5, "window": 8},
                ],
            }
        )
        assert _run(con, long_window, start, end)["open_position"] is None
    finally:
        con.close()

    con = _insert(quiet, symbol="QUIET", start=start)
    try:
        end = start + timedelta(days=len(quiet) - 1)
        gated["long"]["conditions"]  # same rule object is fine on a new book
        result = _run(con, gated, start, end, symbol="QUIET")
        assert result["metrics"]["trade_count"] == 0
        assert result["open_position"] is None
    finally:
        con.close()


def test_rsi_period_and_threshold_round_trip():
    """Wilder RSI(2) on a rise, a five-bar pullback, then a rebound.

    Bar 16 RSI(2) is about 9.76 (passes <= 10, fails <= 5).
    Bar 17 RSI(2) is about 4.94 (passes <= 5). RSI(14) stays above 70 the whole dip.
    Bar 18 rebounds through 70 but not 95; bar 20 clears 95.
    Exit on RSI >= 70 closes the next open; exit on RSI >= 95 waits two more bars.
    The Connors SMA(5) exit fires on the same rebound bar as RSI >= 70.
    """
    start = date(2024, 1, 2)
    closes = [round(20 + 0.4 * i, 10) for i in range(13)]
    price = closes[-1]
    for step in (-0.3, -0.3, -0.3, -0.2, -0.25, 0.6, 1.2, 1.8, 2.0):
        price = round(price + step, 10)
        closes.append(price)
    rows = [(c, c, c, c, 1_000_000) for c in closes]
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)

        def rsi_rules(period, entry, exit_threshold):
            return _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "rsi_lte", "period": period, "threshold": entry}
                    ],
                },
                flat={
                    "logic": "OR",
                    "conditions": [
                        {"type": "rsi_gte", "period": period, "threshold": exit_threshold}
                    ],
                },
            )

        connors = _run(con, rsi_rules(2, 5, 70), start, end)
        assert connors["metrics"]["trade_count"] == 1
        assert connors["open_position"] is None
        trade = connors["trades"][0]
        assert trade["side"] == "long"
        assert trade["entry_date"] == _day(start, 18)
        assert trade["exit_date"] == _day(start, 19)
        assert trade["entry_price"] == pytest.approx(closes[18])
        assert trade["exit_price"] == pytest.approx(closes[19])

        looser_entry = _run(con, rsi_rules(2, 10, 70), start, end)
        assert looser_entry["trades"][0]["entry_date"] == _day(start, 17)
        assert looser_entry["trades"][0]["exit_date"] == _day(start, 19)

        later_exit = _run(con, rsi_rules(2, 5, 95), start, end)
        assert later_exit["trades"][0]["entry_date"] == _day(start, 18)
        assert later_exit["trades"][0]["exit_date"] == _day(start, 21)

        slow = _run(con, rsi_rules(14, 5, 70), start, end)
        assert slow["metrics"]["trade_count"] == 0
        assert slow["open_position"] is None

        sma_exit = _rules(
            long={"logic": "AND", "conditions": [{"type": "rsi_lte", "period": 2, "threshold": 5}]},
            flat={"logic": "OR", "conditions": [{"type": "price_gt_ma", "window": 5}]},
        )
        connors_sma = _run(con, sma_exit, start, end)
        assert connors_sma["trades"][0]["entry_date"] == _day(start, 18)
        assert connors_sma["trades"][0]["exit_date"] == _day(start, 19)
    finally:
        con.close()


def test_donchian_uses_configured_entry_and_exit_lookbacks():
    """Bar 0 high is 20. Bar 5 closes at 11.5.

    N=3 sees only the quiet highs of 10 and enters. N=5 still sees the 20 and stays flat.
    Bar 6 leaves a low of 8. M=2 ignores it once it ages out, so bar 9 (close 10.5)
    breaks the prior two lows of 11. M=4 still contains that 8 and does not exit.
    """
    start = date(2024, 3, 1)
    bars = [
        (19.5, 20.0, 19.0, 19.5),
        (9.5, 10.0, 9.0, 9.5),
        (9.5, 10.0, 9.0, 9.5),
        (9.5, 10.0, 9.0, 9.5),
        (9.5, 10.0, 9.0, 9.5),
        (11.0, 12.0, 11.0, 11.5),
        (11.5, 12.0, 8.0, 11.4),
        (11.4, 12.0, 11.0, 11.4),
        (11.4, 12.0, 11.0, 11.4),
        (11.0, 11.0, 10.0, 10.5),
        (10.5, 11.0, 10.0, 10.4),
    ]
    rows = [(o, h, l, c, 5000) for o, h, l, c in bars]
    con = _insert(rows, start=start)
    try:
        end = start + timedelta(days=len(rows) - 1)

        def channel(entry_n, exit_m):
            return _rules(
                long={
                    "logic": "AND",
                    "conditions": [{"type": "donchian_high", "window": entry_n}],
                },
                flat={
                    "logic": "OR",
                    "conditions": [{"type": "donchian_low", "window": exit_m}],
                },
            )

        classic = _run(con, channel(3, 2), start, end)
        assert classic["metrics"]["trade_count"] == 1
        assert classic["open_position"] is None
        trade = classic["trades"][0]
        assert trade["entry_date"] == _day(start, 6)
        assert trade["exit_date"] == _day(start, 10)
        assert trade["entry_price"] == 11.5
        assert trade["exit_price"] == 10.5

        wide_entry = _run(con, channel(5, 2), start, end)
        assert wide_entry["metrics"]["trade_count"] == 0
        assert wide_entry["open_position"] is None

        wide_exit = _run(con, channel(3, 4), start, end)
        assert wide_exit["metrics"]["trade_count"] == 0
        assert wide_exit["open_position"]["entry_date"] == _day(start, 6)
        assert wide_exit["open_position"]["side"] == "long"
    finally:
        con.close()


def test_api_persists_predicate_knobs_and_backtests_them():
    start = date(2024, 4, 1)
    closes = [10, 11, 12, 13, 14, 15, 16, 17]
    volumes = [100, 100, 100, 100, 100, 100, 1000, 100]
    _insert([(c, c, c, c, v) for c, v in zip(closes, volumes)], start=start)
    client = TestClient(app)
    created = client.post(
        "/strategies",
        json={
            "name": "Volume gate",
            "symbol": "aapl",
            "timeframe": "daily",
            "rules": _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "rel_volume"},
                        {"type": "rsi_lte", "period": 2, "threshold": 5},
                        {"type": "donchian_high"},
                    ],
                },
                flat={"logic": "OR", "conditions": [{"type": "donchian_low", "window": 10}]},
            ),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["rules"]["long"]["conditions"][0] == {"type": "rel_volume", "k": 1.5, "window": 20}
    assert body["rules"]["long"]["conditions"][1] == {
        "type": "rsi_lte",
        "period": 2,
        "threshold": 5.0,
    }
    assert body["rules"]["long"]["conditions"][2] == {"type": "donchian_high", "window": 20}
    assert body["rules"]["flat"]["conditions"][0] == {"type": "donchian_low", "window": 10}

    updated = client.put(
        f"/strategies/{body['id']}",
        json={
            "name": "Volume gate",
            "symbol": "AAPL",
            "timeframe": "daily",
            "rules": _rules(
                long={
                    "logic": "AND",
                    "conditions": [
                        {"type": "price_gt_ma", "window": 5},
                        {"type": "rel_volume", "k": 1.5, "window": 3},
                    ],
                }
            ),
        },
    )
    assert updated.status_code == 200, updated.text
    run = client.post(
        f"/strategies/{body['id']}/backtest",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(days=len(closes) - 1)).isoformat(),
            "costs": ZERO_COSTS,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["open_position"]["entry_date"] == _day(start, 7)

    rejected = client.post(
        "/strategies",
        json={
            "name": "bad k",
            "symbol": "AAPL",
            "rules": _rules(
                long={"logic": "AND", "conditions": [{"type": "rel_volume", "k": -1}]}
            ),
        },
    )
    assert rejected.status_code == 400


def test_research_panel_exposes_predicate_knobs_and_presets():
    script = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    for token in ("rel_volume", "rsi_lte", "rsi_gte", "donchian_high", "donchian_low"):
        assert token in script
    assert "PREDICATE_DEFAULTS" in script
    assert "k: 1.5" in script and "window: 20" in script
    assert "threshold: 5" in script and "threshold: 70" in script
    assert 'data-preset="relvol"' in html
    assert 'data-preset="rsi"' in html
    assert 'data-preset="donchian"' in html
    assert "cond-k" in script and "cond-threshold" in script and "cond-n" in script

from datetime import date

from dixon.backtest import Portfolio, run_backtest
from dixon.costs import CostModel
from dixon.db import connect
from dixon.golden_fixture import golden_failures, run_golden


def test_golden_backtest_pnl_is_stable():
    result = run_golden()
    problems = golden_failures(result)
    assert problems == []
    assert result["metrics"]["win_rate"] == 0.0
    assert result["metrics"]["win_rate_meets_target"] is False
    assert result["metrics"]["max_drawdown_meets_target"] is True
    assert result["currency"] == "USD"


def test_hk_backtest_starts_with_hkd_capital_and_no_trade():
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO daily_bars
              (market, symbol, trade_date, open, high, low, close, volume)
            VALUES ('HK', '0700.HK', ?, 100, 101, 99, 100, 1000)
            """,
            [date(2024, 6, 3)],
        )
        result = run_backtest(
            con,
            {
                "id": "hk",
                "name": "flat",
                "symbol": "0700.HK",
                "market": "HK",
                "timeframe": "daily",
                "rules": {
                    "long": {"logic": "AND", "conditions": []},
                    "flat": {"logic": "AND", "conditions": []},
                    "short": {"logic": "AND", "conditions": []},
                },
            },
            date(2024, 6, 3),
            date(2024, 6, 3),
            None,
        )
    finally:
        con.close()
    assert result["currency"] == "HKD"
    assert result["starting_capital"] == 500_000
    assert result["metrics"]["ending_equity"] == 500_000
    assert result["metrics"]["trade_count"] == 0


def test_synthetic_short_credits_proceeds_and_covers_lower():
    costs = CostModel(
        hk_commission_bps=0,
        hk_spread_ticks=0,
        us_commission_per_share=0,
        us_commission_min=0,
        us_spread_bps=0,
        equity_fraction=0.20,
    )
    book = Portfolio(cash=100_000, costs=costs, market="US")
    when = date(2024, 2, 1)
    assert book.try_open("AAPL", "short", 50.0, when, {"AAPL": 50.0})
    assert abs(book.cash - 120_000) < 1e-6
    book.close_position("AAPL", 40.0, date(2024, 2, 2), "signal")
    assert abs(book.cash - 104_000) < 1e-6
    assert abs(book.trades[0]["pnl"] - 4_000) < 1e-6

    con = connect()
    try:
        start = date(2024, 3, 1)
        for i, px in enumerate([10, 10, 10, 10, 10, 8, 8, 12, 12]):
            con.execute(
                """
                INSERT INTO daily_bars
                  (market, symbol, trade_date, open, high, low, close, volume)
                VALUES ('US', 'MSFT', ?, ?, ?, ?, ?, 1000)
                """,
                [start.fromordinal(start.toordinal() + i), px, px, px, px],
            )
        result = run_backtest(
            con,
            {
                "id": "short",
                "name": "short SMA5",
                "symbol": "MSFT",
                "market": "US",
                "timeframe": "daily",
                "rules": {
                    "long": {"logic": "AND", "conditions": []},
                    "flat": {
                        "logic": "AND",
                        "conditions": [{"type": "price_gt_ma", "window": 5}],
                    },
                    "short": {
                        "logic": "AND",
                        "conditions": [{"type": "price_lt_ma", "window": 5}],
                    },
                },
            },
            start,
            start.fromordinal(start.toordinal() + 8),
            costs.to_dict(),
        )
    finally:
        con.close()
    assert result["metrics"]["trade_count"] == 1
    assert result["trades"][0]["side"] == "short"
    assert result["trades"][0]["entry_price"] == 8
    assert result["trades"][0]["exit_price"] == 12
    assert abs(result["metrics"]["total_pnl"] - (-10_000)) < 0.05


def test_book_caps_concurrent_entries_at_five():
    costs = CostModel(
        hk_commission_bps=0,
        hk_spread_ticks=0,
        us_commission_per_share=0,
        us_commission_min=0,
        us_spread_bps=0,
        equity_fraction=0.20,
    )
    book = Portfolio(cash=100_000, costs=costs, market="US")
    when = date(2024, 1, 2)
    for i in range(5):
        marks = {f"S{j}": 10.0 for j in range(i)}
        marks[f"S{i}"] = 10.0
        assert book.try_open(f"S{i}", "long", 10.0, when, marks)
    marks = {f"S{j}": 10.0 for j in range(6)}
    assert book.try_open("S5", "long", 10.0, when, marks) is False
    assert len(book.positions) == 5


def test_volume_pane_reserves_bottom_quarter():
    from pathlib import Path

    js = Path("dixon/static/app.js").read_text()
    assert 'priceScale("right")' in js
    assert "bottom: 0.25" in js
    assert "top: 0.75" in js
    html = Path("dixon/static/index.html").read_text()
    for window in (5, 9, 20, 30, 50, 100, 250):
        assert f"MA_{window}" in html

"""Reproducible one-trade backtest used by tests and scripts/golden_backtest.py.

Ten AAPL sessions. Long when close > SMA(5), flat when close < SMA(5).
Default US costs. Hand check (fractional shares, no rounding of the ticket):

  signal 2024-01-09 close 12 > SMA5 10.4 → buy the 2024-01-10 open at 12
  signal 2024-01-12 close 8 < SMA5 10.8 → sell the 2024-01-15 open at 8
  shares = 0.20 * 100_000 / 12
  price P&L = (8 - 12) * shares = -20_000 / 3
  costs = US commission both sides + 5 bps slippage both sides = 100 / 3
  net = -6_700
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from dixon.backtest import run_backtest
from dixon.db import connect

GOLDEN_BARS: tuple[tuple[str, float], ...] = (
    ("2024-01-02", 10.0),
    ("2024-01-03", 10.0),
    ("2024-01-04", 10.0),
    ("2024-01-05", 10.0),
    ("2024-01-08", 10.0),
    ("2024-01-09", 12.0),
    ("2024-01-10", 12.0),
    ("2024-01-11", 12.0),
    ("2024-01-12", 8.0),
    ("2024-01-15", 8.0),
)

EXPECTED_TRADE_COUNT = 1
EXPECTED_PNL = -6700.0
EXPECTED_ENDING_EQUITY = 93300.0
EXPECTED_MAX_DRAWDOWN = 0.067
EXPECTED_ENTRY_DATE = "2024-01-10"
EXPECTED_EXIT_DATE = "2024-01-15"
EXPECTED_ENTRY_PRICE = 12.0
EXPECTED_EXIT_PRICE = 8.0
PNL_ABS_TOL = 0.05
DD_ABS_TOL = 1e-6


def run_golden(db_path: Path | None = None) -> dict:
    path = db_path or Path(tempfile.mkdtemp(prefix="dixon-golden-")) / "golden.duckdb"
    con = connect(path)
    try:
        for ds, px in GOLDEN_BARS:
            con.execute(
                """
                INSERT INTO daily_bars
                  (market, symbol, trade_date, open, high, low, close, volume)
                VALUES ('US', 'AAPL', ?, ?, ?, ?, ?, 1000000)
                """,
                [date.fromisoformat(ds), px, px, px, px],
            )
        strategy = {
            "id": "golden",
            "name": "Golden SMA5",
            "symbol": "AAPL",
            "market": "US",
            "timeframe": "daily",
            "rules": {
                "long": {
                    "logic": "AND",
                    "conditions": [{"type": "price_gt_ma", "window": 5}],
                },
                "flat": {
                    "logic": "AND",
                    "conditions": [{"type": "price_lt_ma", "window": 5}],
                },
                "short": {"logic": "AND", "conditions": []},
            },
        }
        return run_backtest(
            con,
            strategy,
            date(2024, 1, 2),
            date(2024, 1, 15),
            None,
        )
    finally:
        con.close()


def golden_failures(result: dict) -> list[str]:
    """Human-readable mismatches. Empty means the fixture still holds."""
    problems: list[str] = []
    metrics = result["metrics"]
    trades = result["trades"]
    if metrics["trade_count"] != EXPECTED_TRADE_COUNT:
        problems.append(f"trade_count {metrics['trade_count']} != {EXPECTED_TRADE_COUNT}")
    if abs(metrics["total_pnl"] - EXPECTED_PNL) > PNL_ABS_TOL:
        problems.append(f"total_pnl {metrics['total_pnl']} != {EXPECTED_PNL}")
    if abs(metrics["ending_equity"] - EXPECTED_ENDING_EQUITY) > PNL_ABS_TOL:
        problems.append(
            f"ending_equity {metrics['ending_equity']} != {EXPECTED_ENDING_EQUITY}"
        )
    if abs(metrics["max_drawdown"] - EXPECTED_MAX_DRAWDOWN) > DD_ABS_TOL:
        problems.append(
            f"max_drawdown {metrics['max_drawdown']} != {EXPECTED_MAX_DRAWDOWN}"
        )
    if len(trades) != 1:
        problems.append(f"expected 1 trade row, got {len(trades)}")
        return problems
    trade = trades[0]
    if trade["entry_date"] != EXPECTED_ENTRY_DATE:
        problems.append(f"entry_date {trade['entry_date']} != {EXPECTED_ENTRY_DATE}")
    if trade["exit_date"] != EXPECTED_EXIT_DATE:
        problems.append(f"exit_date {trade['exit_date']} != {EXPECTED_EXIT_DATE}")
    if abs(trade["entry_price"] - EXPECTED_ENTRY_PRICE) > 1e-9:
        problems.append(f"entry_price {trade['entry_price']} != {EXPECTED_ENTRY_PRICE}")
    if abs(trade["exit_price"] - EXPECTED_EXIT_PRICE) > 1e-9:
        problems.append(f"exit_price {trade['exit_price']} != {EXPECTED_EXIT_PRICE}")
    if trade["side"] != "long":
        problems.append(f"side {trade['side']} != long")
    if result.get("open_position") is not None:
        problems.append("expected a flat book at the end")
    return problems

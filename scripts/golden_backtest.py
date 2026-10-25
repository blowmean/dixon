#!/usr/bin/env python3
"""Run the sealed golden backtest and exit non-zero if P&L drifts.

  python scripts/golden_backtest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dixon.golden_fixture import golden_failures, run_golden  # noqa: E402


def main() -> int:
    result = run_golden()
    problems = golden_failures(result)
    summary = {
        "trade_count": result["metrics"]["trade_count"],
        "total_pnl": result["metrics"]["total_pnl"],
        "ending_equity": result["metrics"]["ending_equity"],
        "max_drawdown": result["metrics"]["max_drawdown"],
        "win_rate": result["metrics"]["win_rate"],
        "trades": result["trades"],
    }
    print(json.dumps(summary, indent=2))
    if problems:
        print("GOLDEN FAIL", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("GOLDEN OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

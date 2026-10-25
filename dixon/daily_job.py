"""Orchestrate rate-limited daily catch-up → targeted rollup → indicators.

Exit codes:
  0  success (all symbols ok; or nothing to do)
  1  fatal error (DB/config/unexpected)
  2  partial success (some symbols failed ingest; rollup/indicators ran for affected)

Usage:
  python -m dixon.daily_job
  python -m dixon.daily_job --restate-days 14 --parquet
  python -m dixon.daily_job --symbols AAPL,0700.HK --restate-days 7
  ./scripts/daily_job.sh
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dixon.compute_indicators import compute_all
from dixon.config import DB_PATH, RESTATE_DAYS
from dixon.ingest import daily_catchup, load_symbol_list
from dixon.config import GOLDEN_FILE, DAY1_FILE
from dixon.rollup import rollup

log = logging.getLogger("dixon.daily_job")


def run_daily_job(
    db_path: Path | None = None,
    restate_days: int | None = None,
    write_parquet: bool = False,
    symbols: list[str] | None = None,
    force_rebuild_all_ok: bool = False,
) -> dict:
    """Run ingest → rollup(affected) → indicators(affected).

    force_rebuild_all_ok: if True, rebuild W/M+indicators for every symbol that
    ingested successfully even when OHLC unchanged (useful after formula changes).
    Default False — only symbols with inserts/updates (restates or new bars).
    """
    db_path = db_path or DB_PATH
    days = RESTATE_DAYS if restate_days is None else restate_days

    log.info("=== Dixon daily job start (restate_days=%s db=%s) ===", days, db_path)
    ingest_out = daily_catchup(
        db_path=db_path,
        write_parquet=write_parquet,
        restate_days=days,
        symbols=symbols,
    )

    if force_rebuild_all_ok:
        affected = [
            (r["market"], r["symbol"])
            for r in ingest_out.get("results", [])
            if r.get("status") == "ok"
        ]
    else:
        affected = list(ingest_out.get("affected") or [])

    log.info(
        "Ingest status=%s errors=%d affected=%d",
        ingest_out.get("status"),
        len(ingest_out.get("errors") or []),
        len(affected),
    )

    rollup_out = None
    indicators_out = None
    if affected:
        log.info(
            "Rebuilding weekly/monthly for: %s",
            ", ".join(f"{m}:{s}" for m, s in affected),
        )
        rollup_out = rollup(affected, db_path=db_path, write_parquet=write_parquet)
        log.info("Computing indicators for %d symbols", len(affected))
        indicators_out = compute_all(affected, db_path=db_path)
    else:
        log.info("No changed symbols — skipping rollup and indicators")

    status = ingest_out.get("status", "ok")
    summary = {
        "status": status,
        "restate_days": days,
        "ingest": ingest_out,
        "affected": [{"market": m, "symbol": s} for m, s in affected],
        "rollup": rollup_out,
        "indicators": indicators_out,
    }
    log.info("=== Dixon daily job done status=%s ===", status)
    return summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Dixon daily catch-up + rollup + indicators")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument(
        "--restate-days",
        type=int,
        default=None,
        help=f"Calendar days to re-fetch/overwrite (default {RESTATE_DAYS})",
    )
    p.add_argument("--parquet", action="store_true")
    p.add_argument("--symbols", type=str, default="", help="Limit to these tickers")
    p.add_argument("--golden", action="store_true")
    p.add_argument("--day1", action="store_true")
    p.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Rebuild W/M+indicators for all successfully ingested symbols even if unchanged",
    )
    args = p.parse_args()

    symbols: list[str] | None = None
    if args.symbols or args.golden or args.day1:
        symbols = []
        if args.symbols:
            symbols.extend(s.strip().upper() for s in args.symbols.split(",") if s.strip())
        if args.golden:
            symbols.extend(load_symbol_list(GOLDEN_FILE))
        if args.day1:
            symbols.extend(load_symbol_list(DAY1_FILE))
        # dedupe
        seen: set[str] = set()
        uniq: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        symbols = uniq

    try:
        out = run_daily_job(
            db_path=args.db,
            restate_days=args.restate_days,
            write_parquet=args.parquet,
            symbols=symbols,
            force_rebuild_all_ok=args.force_rebuild,
        )
    except Exception:
        log.exception("Daily job fatal error")
        sys.exit(1)

    # Compact stdout summary for cron logs
    print(
        {
            "status": out["status"],
            "restate_days": out["restate_days"],
            "affected": out["affected"],
            "ingest_errors": out["ingest"].get("errors"),
            "indicators": out.get("indicators"),
        }
    )

    if out["status"] == "error":
        sys.exit(1)
    if out["status"] == "partial":
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()

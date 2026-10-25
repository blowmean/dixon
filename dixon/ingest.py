"""Yahoo ingest → DuckDB daily_bars (+ optional Hive Parquet).

Usage:
  python -m dixon.ingest --symbols 0700.HK,AAPL --years 5
  python -m dixon.ingest --universe universe/golden.txt --parquet
  python -m dixon.ingest --phase-a --years 5 --parquet
  python -m dixon.ingest --all --years 5 --parquet
  python -m dixon.ingest --daily --restate-days 14
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from dixon.calendars import default_history_window
from dixon.config import (
    ALL_FILE,
    DB_PATH,
    DAY1_FILE,
    GOLDEN_FILE,
    PHASE_A_FILE,
    HISTORY_YEARS,
    PARQUET_ROOT,
    RESTATE_DAYS,
)
from dixon.db import connect
from dixon.yahoo import download_adjusted_daily, fetch_meta, infer_asset_class, infer_market

log = logging.getLogger("dixon.ingest")


def load_symbol_list(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sym = line.split("#", 1)[0].strip().upper()
        if sym:
            out.append(sym)
    return out


def upsert_symbol(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    market: str,
    *,
    refresh_meta: bool = True,
) -> None:
    """Insert/update symbol registry. Skip Yahoo meta on daily path when already known."""
    existing = con.execute(
        "SELECT 1 FROM symbols WHERE symbol = ? AND market = ?",
        [symbol, market],
    ).fetchone()
    if existing and not refresh_meta:
        con.execute(
            """
            UPDATE symbols SET is_active = TRUE, updated_at = now()
            WHERE symbol = ? AND market = ?
            """,
            [symbol, market],
        )
        return

    meta = fetch_meta(symbol) if refresh_meta else {}
    currency = meta.get("currency") or ("HKD" if market == "HK" else "USD")
    exchange = meta.get("exchange") or ("HKEX" if market == "HK" else None)
    asset_class = infer_asset_class(symbol)
    con.execute(
        """
        INSERT INTO symbols (symbol, market, exchange, name, currency, asset_class, is_active, yahoo_ticker, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, TRUE, ?, now())
        ON CONFLICT (symbol, market) DO UPDATE SET
          exchange = COALESCE(excluded.exchange, symbols.exchange),
          currency = COALESCE(excluded.currency, symbols.currency),
          asset_class = excluded.asset_class,
          yahoo_ticker = excluded.yahoo_ticker,
          is_active = TRUE,
          updated_at = now()
        """,
        [symbol, market, exchange, meta.get("name"), currency, asset_class, symbol],
    )


def _load_existing_window(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> dict[date, tuple[float, float, float, float, int]]:
    rows = con.execute(
        """
        SELECT trade_date, open, high, low, close, COALESCE(volume, 0)
        FROM daily_bars
        WHERE market = ? AND symbol = ? AND trade_date >= ? AND trade_date <= ?
        """,
        [market, symbol, start, end],
    ).fetchall()
    out: dict[date, tuple[float, float, float, float, int]] = {}
    for r in rows:
        d = r[0]
        if hasattr(d, "isoformat") and not isinstance(d, date):
            d = d.date() if hasattr(d, "date") else d
        out[d] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]))
    return out


def _round_px(x: float) -> float:
    # Tolerate tiny float noise when comparing restates
    return round(float(x), 6)


def diff_bars(
    existing: dict[date, tuple[float, float, float, float, int]],
    df: pd.DataFrame,
) -> tuple[int, int, int]:
    """Return (inserted, updated, unchanged) vs existing window."""
    inserted = updated = unchanged = 0
    if df.empty:
        return 0, 0, 0
    for row in df.itertuples(index=False):
        d = row.trade_date
        if hasattr(d, "date") and not isinstance(d, date):
            d = d.date()
        new = (
            _round_px(row.open),
            _round_px(row.high),
            _round_px(row.low),
            _round_px(row.close),
            int(row.volume or 0),
        )
        old = existing.get(d)
        if old is None:
            inserted += 1
            continue
        old_r = (
            _round_px(old[0]),
            _round_px(old[1]),
            _round_px(old[2]),
            _round_px(old[3]),
            int(old[4]),
        )
        if old_r != new:
            updated += 1
        else:
            unchanged += 1
    return inserted, updated, unchanged


def write_daily_bars(
    con: duckdb.DuckDBPyConnection,
    market: str,
    symbol: str,
    df: pd.DataFrame,
) -> int:
    if df.empty:
        return 0
    con.register("_bars", df.assign(market=market, symbol=symbol, source="yahoo"))
    con.execute(
        """
        INSERT OR REPLACE INTO daily_bars
          (market, symbol, trade_date, open, high, low, close, volume, source, ingested_at)
        SELECT market, symbol, trade_date, open, high, low, close, volume, source, now()
        FROM _bars
        """
    )
    con.unregister("_bars")
    return len(df)


def export_parquet(
    con: duckdb.DuckDBPyConnection,
    market: str | None,
    timeframe: str,
    parquet_root: Path,
) -> None:
    """Write Hive partitions: market=/timeframe=/year=."""
    parquet_root.mkdir(parents=True, exist_ok=True)
    table = {
        "daily": "daily_bars",
        "weekly": "weekly_bars",
        "monthly": "monthly_bars",
    }[timeframe]
    date_col = "trade_date" if timeframe == "daily" else "period_end"
    where = "WHERE 1=1"
    params: list = []
    if market:
        where += " AND market = ?"
        params.append(market)
    sql = f"""
        COPY (
          SELECT
            market,
            symbol,
            {date_col} AS bar_date,
            open, high, low, close, volume,
            '{timeframe}' AS timeframe,
            year({date_col}) AS year
          FROM {table}
          {where}
        )
        TO ?
        (FORMAT PARQUET, PARTITION_BY (market, timeframe, year), OVERWRITE_OR_IGNORE true)
    """
    params.append(str(parquet_root))
    con.execute(sql, params)


def _start_run(con: duckdb.DuckDBPyConnection, job_type: str) -> str:
    run_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO ingest_runs (run_id, job_type, started_at, status, detail)
        VALUES (?, ?, ?, 'running', NULL)
        """,
        [run_id, job_type, datetime.now()],
    )
    return run_id


def _finish_run(con: duckdb.DuckDBPyConnection, run_id: str, status: str, detail: str) -> None:
    con.execute(
        """
        UPDATE ingest_runs
        SET finished_at = ?, status = ?, detail = ?
        WHERE run_id = ?
        """,
        [datetime.now(), status, detail, run_id],
    )


def active_symbols(db_path: Path | None = None) -> list[str]:
    con = connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT symbol FROM symbols
            WHERE is_active AND asset_class IN ('equity', 'etf')
            ORDER BY market, symbol
            """
        ).fetchall()
    finally:
        con.close()
    symbols = [r[0] for r in rows]
    if symbols:
        return symbols
    if GOLDEN_FILE.exists():
        return load_symbol_list(GOLDEN_FILE)
    return []


def ingest_symbols(
    symbols: list[str],
    years: int = HISTORY_YEARS,
    start: date | None = None,
    end: date | None = None,
    db_path: Path | None = None,
    write_parquet: bool = False,
    job_type: str = "backfill",
    *,
    soft_fail: bool = False,
    refresh_meta: bool = True,
    track_changes: bool = False,
) -> dict:
    """Ingest Yahoo adjusted daily bars.

    soft_fail: per-symbol errors logged; job continues (daily path).
    track_changes: compare window before overwrite; set changed/affected flags.
    """
    con = connect(db_path)
    run_id = _start_run(con, job_type)
    if end is None or start is None:
        s0, e0 = default_history_window(years)
        start = start or s0
        end = end or e0

    results: list[dict] = []
    errors: list[dict] = []
    try:
        for symbol in symbols:
            market = infer_market(symbol)
            log.info("[%s] %s %s → %s", market, symbol, start, end)
            try:
                upsert_symbol(con, symbol, market, refresh_meta=refresh_meta)
                existing = (
                    _load_existing_window(con, market, symbol, start, end)
                    if track_changes
                    else {}
                )
                df = download_adjusted_daily(symbol, start, end)
                inserted = updated = unchanged = 0
                if track_changes:
                    inserted, updated, unchanged = diff_bars(existing, df)
                n = write_daily_bars(con, market, symbol, df)
                changed = (inserted + updated) > 0 if track_changes else n > 0
                rec = {
                    "symbol": symbol,
                    "market": market,
                    "rows": n,
                    "inserted": inserted,
                    "updated": updated,
                    "unchanged": unchanged,
                    "changed": changed,
                    "status": "ok",
                }
                results.append(rec)
                log.info(
                    "  %s rows=%d inserted=%d updated=%d unchanged=%d changed=%s",
                    symbol,
                    n,
                    inserted,
                    updated,
                    unchanged,
                    changed,
                )
            except Exception as e:  # noqa: BLE001 — soft-fail per symbol
                err = {"symbol": symbol, "market": market, "status": "error", "error": str(e)}
                errors.append(err)
                results.append({**err, "rows": 0, "changed": False})
                log.error("  %s FAILED: %s", symbol, e)
                if not soft_fail:
                    raise

        if write_parquet and any(r.get("status") == "ok" and r.get("rows", 0) > 0 for r in results):
            for mkt in sorted({r["market"] for r in results if r.get("status") == "ok"}):
                export_parquet(con, mkt, "daily", PARQUET_ROOT)

        ok_n = sum(1 for r in results if r.get("status") == "ok")
        detail = (
            f"symbols={len(results)} ok={ok_n} errors={len(errors)} "
            f"rows={sum(r.get('rows', 0) for r in results)}"
        )
        status = "ok" if not errors else ("partial" if ok_n else "error")
        _finish_run(con, run_id, status, detail)
        return {
            "run_id": run_id,
            "status": status,
            "results": results,
            "errors": errors,
            "start": str(start),
            "end": str(end),
            "affected": [
                (r["market"], r["symbol"])
                for r in results
                if r.get("status") == "ok" and r.get("changed")
            ],
        }
    except Exception as e:
        _finish_run(con, run_id, "error", str(e))
        raise
    finally:
        con.close()


def daily_catchup(
    db_path: Path | None = None,
    write_parquet: bool = False,
    restate_days: int | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """Incremental refresh with soft restate overwrite window.

    Downloads [today - restate_days, today] for each active symbol and
    INSERT OR REPLACE daily bars (catches Yahoo soft restates). Soft-fails
    per symbol. Returns `affected` list of (market, symbol) that actually
    changed (new or restated OHLC/volume) for targeted rollup/indicators.
    """
    days = RESTATE_DAYS if restate_days is None else int(restate_days)
    if days < 1:
        raise ValueError("restate_days must be >= 1")

    symbols = symbols if symbols is not None else active_symbols(db_path)
    if not symbols:
        log.warning("No active symbols; nothing to do")
        return {
            "status": "ok",
            "results": [],
            "errors": [],
            "affected": [],
            "note": "no active symbols",
            "restate_days": days,
        }

    end = date.today()
    start = end - timedelta(days=days)
    log.info(
        "Daily catch-up: %d symbols, restate window %s → %s (%d calendar days)",
        len(symbols),
        start,
        end,
        days,
    )
    out = ingest_symbols(
        symbols,
        start=start,
        end=end,
        db_path=db_path,
        write_parquet=write_parquet,
        job_type="daily",
        soft_fail=True,
        refresh_meta=False,
        track_changes=True,
    )
    out["restate_days"] = days
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Dixon Yahoo ingest")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--symbols", type=str, default="", help="Comma-separated tickers")
    p.add_argument("--universe", type=Path, default=None, help="Path to symbol list file")
    p.add_argument("--golden", action="store_true", help="Use universe/golden.txt")
    p.add_argument("--day1", action="store_true", help="Use universe/day1.txt")
    p.add_argument("--phase-a", action="store_true", help="Use universe/phase_a.txt (HSI+HSTECH+SPX+NDX+ETFs)")
    p.add_argument("--all", action="store_true", help="Use universe/all.txt (full HK+US)")
    p.add_argument("--years", type=int, default=HISTORY_YEARS)
    p.add_argument("--start", type=str, default=None)
    p.add_argument("--end", type=str, default=None)
    p.add_argument("--parquet", action="store_true")
    p.add_argument("--daily", action="store_true", help="Incremental catch-up + restate window")
    p.add_argument(
        "--restate-days",
        type=int,
        default=None,
        help=f"Calendar days to re-download/overwrite (default {RESTATE_DAYS} / DIXON_RESTATE_DAYS)",
    )
    args = p.parse_args()

    if args.daily:
        syms: list[str] | None = None
        if args.symbols or args.golden or args.day1 or args.phase_a or args.all or args.universe:
            symbols: list[str] = []
            if args.symbols:
                symbols.extend(s.strip().upper() for s in args.symbols.split(",") if s.strip())
            if args.golden:
                symbols.extend(load_symbol_list(GOLDEN_FILE))
            if args.day1:
                symbols.extend(load_symbol_list(DAY1_FILE))
            if args.phase_a:
                symbols.extend(load_symbol_list(PHASE_A_FILE))
            if args.all:
                symbols.extend(load_symbol_list(ALL_FILE))
            if args.universe:
                symbols.extend(load_symbol_list(args.universe))
            seen: set[str] = set()
            uniq: list[str] = []
            for s in symbols:
                if s not in seen:
                    seen.add(s)
                    uniq.append(s)
            syms = uniq
        out = daily_catchup(
            args.db,
            write_parquet=args.parquet,
            restate_days=args.restate_days,
            symbols=syms,
        )
        print(out)
        # exit 0 ok, 2 partial, 1 total failure
        if out.get("status") == "error":
            sys.exit(1)
        if out.get("status") == "partial":
            sys.exit(2)
        return

    symbols_list: list[str] = []
    if args.symbols:
        symbols_list.extend(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if args.golden:
        symbols_list.extend(load_symbol_list(GOLDEN_FILE))
    if args.day1:
        symbols_list.extend(load_symbol_list(DAY1_FILE))
    if args.phase_a:
        symbols_list.extend(load_symbol_list(PHASE_A_FILE))
    if args.all:
        symbols_list.extend(load_symbol_list(ALL_FILE))
    if args.universe:
        symbols_list.extend(load_symbol_list(args.universe))
    seen2: set[str] = set()
    uniq2: list[str] = []
    for s in symbols_list:
        if s not in seen2:
            seen2.add(s)
            uniq2.append(s)
    if not uniq2:
        p.error("Provide --symbols, --golden, --day1, --phase-a, --all, or --universe")

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    # If --restate-days given without --daily, treat as window ending today
    if args.restate_days is not None and start is None:
        end = end or date.today()
        start = end - timedelta(days=args.restate_days)

    out = ingest_symbols(
        uniq2,
        years=args.years,
        start=start,
        end=end,
        db_path=args.db,
        write_parquet=args.parquet,
        soft_fail=False,
        refresh_meta=True,
        track_changes=False,
    )
    print(out)


if __name__ == "__main__":
    main()

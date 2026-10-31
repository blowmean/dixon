"""Overnight US Top-50 strategy board.

Grid version ``s1-v1`` (frozen). Each US Top-50 name is scored on a rolling
12-month window with the single-name next-open backtest and US v1.2 cost
defaults (no overrides). Families are searched separately — there is no
cartesian AND across RSI, relative volume, and Donchian. Public presets are
extra fixed cells (anchors), not a cross of those grids.

Rank within a symbol: higher total P&L, then fewer closed trades, then
``rule_id`` ascending. Persist the top 3 only. ``metrics.total_pnl`` is
ending equity minus starting capital, so an open position's mark is included.
``trade_count`` is closed round-trips only.

A full run replaces the ``(market, grid_version, as_of)`` slice. The same
bars therefore rewrite the same rows.

Usage:
  python -m dixon.strategy_board
  python -m dixon.strategy_board --as-of 2026-10-30
  python -m dixon.strategy_board --symbols AAPL,MSFT
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from dixon.backtest import run_backtest
from dixon.costs import CostModel
from dixon.db import connect
from dixon.rules import validate_rules
from dixon.screener import build_top50, latest_trade_date, to_date
from dixon.yahoo import infer_market

log = logging.getLogger("dixon.strategy_board")

GRID_VERSION = "s1-v1"
COSTS_FLAG = "us-v1.2"
MARKET = "US"
TOP_N = 3
UNIVERSE = "us-top50"

# Connors-style flat used by every RSI-family cell. The entry period is the
# grid axis; the exit RSI uses that same period and the preset's 70 line.
_RSI_EXIT_THRESHOLD = 70.0
_RSI_EXIT_MA = 5
# Public volume breakout wraps relative volume in close vs MA(20).
_RVOL_MA = 20

_RSI_PERIODS = (2, 5, 7, 14)
_RSI_THRESHOLDS = (10, 20, 30)
_RVOL_KS = (1.2, 1.5, 2.0)
_RVOL_WINDOWS = (10, 20)
_DONCHIAN_PAIRS = ((20, 10), (55, 20))


@dataclass(frozen=True)
class GridCell:
    """One complete strategy. ``rules_json`` is the canonical rule tree."""

    rule_id: str
    family: str
    summary: str
    params_json: str
    rules_json: str

    def params(self) -> dict[str, Any]:
        return json.loads(self.params_json)

    def rules(self) -> dict[str, Any]:
        return json.loads(self.rules_json)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _cell(
    rule_id: str,
    family: str,
    summary: str,
    params: dict[str, Any],
    rules: dict[str, Any],
) -> GridCell:
    if family not in {"rsi", "rvol", "donchian", "anchor"}:
        raise RuntimeError(f"unknown family {family}")
    canonical = validate_rules(rules)
    return GridCell(
        rule_id=rule_id,
        family=family,
        summary=summary,
        params_json=_dumps(params),
        rules_json=_dumps(canonical),
    )


def _empty_short() -> dict[str, Any]:
    return {"logic": "AND", "conditions": []}


def rsi_rules(period: int, threshold: int) -> dict[str, Any]:
    """Connors-shaped mean reversion. Entry threshold varies; the flat does not.

    Flat is close > SMA(5) OR RSI(same period) >= 70, matching the public
    Connors preset (that preset's entry is RSI(2) <= 5, which is an anchor,
    not a point on this grid).
    """
    return {
        "long": {
            "logic": "AND",
            "conditions": [{"type": "rsi_lte", "period": period, "threshold": threshold}],
        },
        "flat": {
            "logic": "OR",
            "conditions": [
                {"type": "price_gt_ma", "window": _RSI_EXIT_MA},
                {"type": "rsi_gte", "period": period, "threshold": _RSI_EXIT_THRESHOLD},
            ],
        },
        "short": _empty_short(),
    }


def rvol_rules(k: float, window: int) -> dict[str, Any]:
    """Public volume-breakout shape. Only k and the volume window vary.

    Long when close > MA(20) and volume >= k × SMA(volume, N). Flat when
    close < MA(20). N=10 is not an SMA-menu window, so the price filter stays
    at 20, the same gate the public preset uses.
    """
    return {
        "long": {
            "logic": "AND",
            "conditions": [
                {"type": "price_gt_ma", "window": _RVOL_MA},
                {"type": "rel_volume", "k": k, "window": window},
            ],
        },
        "flat": {
            "logic": "OR",
            "conditions": [{"type": "price_lt_ma", "window": _RVOL_MA}],
        },
        "short": _empty_short(),
    }


def donchian_rules(entry: int, exit_: int) -> dict[str, Any]:
    """Close above the prior N-bar high; flat below the prior M-bar low."""
    return {
        "long": {
            "logic": "AND",
            "conditions": [{"type": "donchian_high", "window": entry}],
        },
        "flat": {
            "logic": "OR",
            "conditions": [{"type": "donchian_low", "window": exit_}],
        },
        "short": _empty_short(),
    }


def anchor_rules() -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """Public presets, in UI order. ROC carries the 5% trail; the others do not.

    Returns (rule_id, summary, params, rules) before canonicalisation.
    """
    return [
        (
            "anchor/relvol",
            "Volume breakout k=1.5 N=20",
            {"preset": "relvol"},
            rvol_rules(1.5, 20),
        ),
        (
            "anchor/connors",
            "Connors RSI(2) ≤ 5; flat SMA(5) or RSI≥70",
            {"preset": "connors"},
            rsi_rules(2, 5),
        ),
        (
            "anchor/donchian",
            "Donchian 20/10",
            {"preset": "donchian"},
            donchian_rules(20, 10),
        ),
        (
            "anchor/roc7",
            "ROC(7) ±1% trail 5%",
            {"preset": "roc7"},
            {
                "long": {
                    "logic": "AND",
                    "conditions": [{"type": "roc_gte", "period": 7, "threshold": 1}],
                },
                "flat": {"logic": "AND", "conditions": []},
                "short": {
                    "logic": "AND",
                    "conditions": [{"type": "roc_lte", "period": 7, "threshold": -1}],
                },
                "trail_pct": 5,
            },
        ),
        (
            "anchor/ema921",
            "EMA 9/21 + volume k=1.5 N=20",
            {"preset": "ema921"},
            {
                "long": {
                    "logic": "AND",
                    "conditions": [
                        {"type": "ema_gt_ema", "fast": 9, "slow": 21},
                        {"type": "rel_volume", "k": 1.5, "window": 20},
                    ],
                },
                "flat": {"logic": "AND", "conditions": []},
                "short": {
                    "logic": "AND",
                    "conditions": [
                        {"type": "ema_lt_ema", "fast": 9, "slow": 21},
                        {"type": "rel_volume", "k": 1.5, "window": 20},
                    ],
                },
            },
        ),
    ]


def grid_cells() -> tuple[GridCell, ...]:
    """Frozen s1-v1 pool. Order is the catalogue order, not the rank order."""
    cells: list[GridCell] = []
    for period in _RSI_PERIODS:
        for threshold in _RSI_THRESHOLDS:
            cells.append(
                _cell(
                    f"rsi/p{period}/lte{threshold}",
                    "rsi",
                    (
                        f"RSI({period}) ≤ {threshold}; "
                        f"flat close>SMA({_RSI_EXIT_MA}) or RSI({period})≥70"
                    ),
                    {
                        "period": period,
                        "rsi_lte": threshold,
                        "exit": f"price_gt_ma:{_RSI_EXIT_MA}|rsi_gte:70",
                    },
                    rsi_rules(period, threshold),
                )
            )
    for k in _RVOL_KS:
        for window in _RVOL_WINDOWS:
            cells.append(
                _cell(
                    f"rvol/k{k:.1f}/n{window}",
                    "rvol",
                    (
                        f"RVOL k={k:.1f} N={window}; "
                        f"long close>MA{_RVOL_MA} and volume; flat close<MA{_RVOL_MA}"
                    ),
                    {"k": k, "window": window, "long_ma": _RVOL_MA, "flat_ma": _RVOL_MA},
                    rvol_rules(k, window),
                )
            )
    for entry, exit_ in _DONCHIAN_PAIRS:
        cells.append(
            _cell(
                f"donchian/n{entry}/m{exit_}",
                "donchian",
                f"Donchian high {entry} / low {exit_}",
                {"entry_high": entry, "exit_low": exit_},
                donchian_rules(entry, exit_),
            )
        )
    for rule_id, summary, params, rules in anchor_rules():
        cells.append(_cell(rule_id, "anchor", summary, params, rules))
    ids = [c.rule_id for c in cells]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate rule_id in s1-v1 grid")
    return tuple(cells)


def rolling_window(as_of: date) -> tuple[date, date]:
    """Inclusive window from the calendar date 12 months earlier through ``as_of``.

    29 Feb rolls back to 28 Feb. Indicator warm-up still sees bars stored
    before ``window_start``; only signals inside the window are filled.
    """
    try:
        start = date(as_of.year - 1, as_of.month, as_of.day)
    except ValueError:
        start = date(as_of.year - 1, as_of.month, 28)
    return start, as_of


def rank_rows(rows: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Highest total P&L, then fewer trades, then rule_id. Ranks start at 1."""
    ordered = sorted(
        rows,
        key=lambda r: (
            -round(float(r["total_pnl"]), 6),
            int(r["trade_count"]),
            str(r["rule_id"]),
        ),
    )
    ranked: list[dict[str, Any]] = []
    for rank, row in enumerate(ordered[:top_n], start=1):
        chosen = dict(row)
        chosen["rank"] = rank
        ranked.append(chosen)
    return ranked


def score_symbol(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    *,
    window_start: date,
    window_end: date,
    market: str = MARKET,
) -> list[dict[str, Any]]:
    """Score every s1-v1 cell. Costs are US v1.2 defaults (``cost_overrides`` omitted)."""
    if market != MARKET:
        raise ValueError("strategy board scores US names only")
    scored: list[dict[str, Any]] = []
    for cell in grid_cells():
        result = run_backtest(
            con,
            {
                "id": cell.rule_id,
                "name": cell.summary,
                "symbol": symbol,
                "market": market,
                "timeframe": "daily",
                "rules": cell.rules(),
            },
            window_start,
            window_end,
            None,
        )
        metrics = result["metrics"]
        scored.append(
            {
                "symbol": symbol,
                "market": market,
                "family": cell.family,
                "rule_id": cell.rule_id,
                "summary": cell.summary,
                "params": cell.params(),
                "params_json": cell.params_json,
                "rules": cell.rules(),
                "rules_json": cell.rules_json,
                "total_pnl": round(float(metrics["total_pnl"]), 6),
                "trade_count": int(metrics["trade_count"]),
                "window_start": window_start,
                "window_end": window_end,
                "costs_flag": COSTS_FLAG,
            }
        )
    return scored


def _us_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        if infer_market(symbol) != MARKET:
            raise ValueError(f"{symbol} is not a US symbol; the board is US Top-50 only")
        seen.add(symbol)
        out.append(symbol)
    if not out:
        raise ValueError("symbols is empty")
    return out


def resolve_universe(
    con: duckdb.DuckDBPyConnection,
    as_of: date | None,
    symbols: list[str] | None,
) -> tuple[date | None, list[str], str]:
    """Return ``(as_of, symbols, universe_label)``.

    ``symbols`` set means that explicit list (still US only). Otherwise the
    symbols are that session's US Top-50. ``as_of`` defaults to the latest
    US session in ``daily_bars``.
    """
    if symbols is not None:
        names = _us_symbols(symbols)
        label = "symbols"
    else:
        names = []
        label = UNIVERSE
    resolved = as_of
    if resolved is None:
        resolved = latest_trade_date(con, MARKET)
    if resolved is None:
        return None, [], label
    if symbols is None:
        pool = build_top50(con, MARKET, resolved)
        names = sorted({str(row["symbol"]) for row in pool["rows"]})
    return resolved, names, label


def write_board_rows(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    *,
    symbols: list[str] | None = None,
) -> None:
    """Replace a board slice, then insert ``rows``.

    ``symbols is None`` deletes the whole ``(market, grid_version, as_of)``
    slice (the nightly run). A list deletes only those symbols so a one-name
    recompute leaves the rest of that night in place. Every row in one call
    must share market, grid_version, and as_of.
    """
    if not rows and symbols is None:
        raise ValueError("refusing to clear a board slice with no replacement rows and no symbol list")
    if rows:
        markets = {r["market"] for r in rows}
        versions = {r["grid_version"] for r in rows}
        as_ofs = {to_date(r["as_of"]) for r in rows}
        if len(markets) != 1 or len(versions) != 1 or len(as_ofs) != 1:
            raise ValueError("board rows in one write must share market, grid_version, and as_of")
        market = next(iter(markets))
        version = next(iter(versions))
        as_of = next(iter(as_ofs))
    else:
        # Partial clear of symbols that scored nothing is not used; callers
        # always pass the slice keys via rows. Keep the signature honest.
        raise ValueError("board write needs rows")

    payloads = []
    for row in rows:
        rank = int(row["rank"])
        if rank < 1 or rank > TOP_N:
            raise ValueError(f"rank {rank} is outside 1..{TOP_N}")
        payloads.append(
            [
                market,
                version,
                as_of,
                str(row["symbol"]),
                rank,
                str(row["family"]),
                str(row["rule_id"]),
                str(row["rules_json"]),
                str(row["params_json"]),
                str(row["summary"]),
                round(float(row["total_pnl"]), 6),
                int(row["trade_count"]),
                to_date(row["window_start"]),
                to_date(row["window_end"]),
                str(row["costs_flag"]),
            ]
        )

    con.execute("BEGIN TRANSACTION")
    try:
        if symbols is None:
            con.execute(
                """
                DELETE FROM strategy_board
                WHERE market = ? AND grid_version = ? AND as_of = ?
                """,
                [market, version, as_of],
            )
        else:
            names = _us_symbols(symbols)
            placeholders = ", ".join("?" for _ in names)
            con.execute(
                f"""
                DELETE FROM strategy_board
                WHERE market = ? AND grid_version = ? AND as_of = ?
                  AND symbol IN ({placeholders})
                """,
                [market, version, as_of, *names],
            )
        con.executemany(
            """
            INSERT INTO strategy_board (
              market, grid_version, as_of, symbol, rank, family, rule_id,
              rules_json, params_json, summary, total_pnl, trade_count,
              window_start, window_end, costs_flag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payloads,
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def run_board(
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    db_path: Path | None = None,
    as_of: date | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Score the universe and replace that night's slice.

    Pass ``symbols`` only to recompute a US subset. The nightly path leaves
    it unset and ranks the US Top-50 for ``as_of`` (default: latest US session).
    """
    own = con is None
    if con is None:
        con = connect(db_path)
    started = time.perf_counter()
    try:
        resolved, names, label = resolve_universe(con, as_of, symbols)
        if resolved is None:
            log.warning("no US session in daily_bars; strategy board skipped")
            return {
                "status": "no_session",
                "grid_version": GRID_VERSION,
                "market": MARKET,
                "universe": label,
                "as_of": None,
                "symbol_count": 0,
                "row_count": 0,
                "costs_flag": COSTS_FLAG,
            }
        window_start, window_end = rolling_window(resolved)
        if not names:
            # A full re-run with an empty Top-50 clears that night so the
            # board does not keep names that have left the pool. A symbol
            # override never reaches here with an empty list.
            if symbols is None:
                con.execute("BEGIN TRANSACTION")
                try:
                    con.execute(
                        """
                        DELETE FROM strategy_board
                        WHERE market = ? AND grid_version = ? AND as_of = ?
                        """,
                        [MARKET, GRID_VERSION, resolved],
                    )
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            log.warning("US Top-50 is empty for %s", resolved.isoformat())
            return {
                "status": "empty_universe",
                "grid_version": GRID_VERSION,
                "market": MARKET,
                "universe": label,
                "as_of": resolved.isoformat(),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "symbol_count": 0,
                "row_count": 0,
                "costs_flag": COSTS_FLAG,
            }
        log.info(
            "strategy board %s %s symbols=%d window=%s..%s costs=%s",
            GRID_VERSION,
            resolved.isoformat(),
            len(names),
            window_start.isoformat(),
            window_end.isoformat(),
            COSTS_FLAG,
        )
        stored: list[dict[str, Any]] = []
        for i, symbol in enumerate(names, start=1):
            log.info("scoring %s (%d/%d)", symbol, i, len(names))
            top = rank_rows(
                score_symbol(
                    con,
                    symbol,
                    window_start=window_start,
                    window_end=window_end,
                    market=MARKET,
                )
            )
            for row in top:
                stored.append(
                    {
                        **row,
                        "grid_version": GRID_VERSION,
                        "as_of": resolved,
                    }
                )
        write_board_rows(con, stored, symbols=names if symbols is not None else None)
        elapsed = round(time.perf_counter() - started, 3)
        summary = {
            "status": "ok",
            "grid_version": GRID_VERSION,
            "market": MARKET,
            "universe": label,
            "as_of": resolved.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "costs_flag": COSTS_FLAG,
            "symbol_count": len(names),
            "row_count": len(stored),
            "elapsed_sec": elapsed,
        }
        log.info("strategy board done %s", summary)
        return summary
    finally:
        if own:
            con.close()


def _public_row(record: tuple[Any, ...]) -> dict[str, Any]:
    (
        as_of,
        grid_version,
        symbol,
        market,
        rank,
        family,
        rule_id,
        rules_json,
        params_json,
        summary,
        total_pnl,
        trade_count,
        window_start,
        window_end,
        costs_flag,
    ) = record
    return {
        "as_of": to_date(as_of).isoformat(),
        "grid_version": grid_version,
        "symbol": symbol,
        "market": market,
        "rank": int(rank),
        "family": family,
        "rule_id": rule_id,
        "summary": summary,
        "params": json.loads(params_json),
        "rules": json.loads(rules_json),
        "total_pnl": round(float(total_pnl), 6),
        "trade_count": int(trade_count),
        "window_start": to_date(window_start).isoformat(),
        "window_end": to_date(window_end).isoformat(),
        "costs_flag": costs_flag,
    }


def _empty_payload(
    *,
    market: str,
    grid_version: str,
    as_of: date | None,
) -> dict[str, Any]:
    return {
        "market": market,
        "grid_version": grid_version,
        "universe": UNIVERSE,
        "as_of": as_of.isoformat() if as_of else None,
        "window_start": None,
        "window_end": None,
        "costs_flag": None,
        "symbol_count": 0,
        "count": 0,
        "top_n": TOP_N,
        "rows": [],
    }


def read_board(
    con: duckdb.DuckDBPyConnection,
    *,
    market: str = MARKET,
    as_of: date | None = None,
    grid_version: str = GRID_VERSION,
) -> dict[str, Any]:
    """Board rows for one night. ``as_of`` omitted means the latest stored night.

    A requested night with no rows stays empty (the UI can say the job has
    not been run for that date). Omitting ``as_of`` when nothing is stored
    returns ``as_of: null``.
    """
    market_u = market.strip().upper()
    if market_u != MARKET:
        raise ValueError("strategy board is US Top-50 only")
    version = grid_version.strip() or GRID_VERSION
    explicit = as_of is not None
    resolved = as_of
    if resolved is None:
        found = con.execute(
            """
            SELECT MAX(as_of) FROM strategy_board
            WHERE market = ? AND grid_version = ?
            """,
            [market_u, version],
        ).fetchone()
        if found and found[0] is not None:
            resolved = to_date(found[0])
    if resolved is None:
        return _empty_payload(market=market_u, grid_version=version, as_of=None)
    records = con.execute(
        """
        SELECT as_of, grid_version, symbol, market, rank, family, rule_id,
               rules_json, params_json, summary, total_pnl, trade_count,
               window_start, window_end, costs_flag
        FROM strategy_board
        WHERE market = ? AND grid_version = ? AND as_of = ?
        ORDER BY symbol, rank
        """,
        [market_u, version, resolved],
    ).fetchall()
    if not records:
        return _empty_payload(
            market=market_u,
            grid_version=version,
            as_of=resolved if explicit else None,
        )
    rows = [_public_row(rec) for rec in records]
    return {
        "market": market_u,
        "grid_version": version,
        "universe": UNIVERSE,
        "as_of": rows[0]["as_of"],
        "window_start": rows[0]["window_start"],
        "window_end": rows[0]["window_end"],
        "costs_flag": rows[0]["costs_flag"],
        "symbol_count": len({r["symbol"] for r in rows}),
        "count": len(rows),
        "top_n": TOP_N,
        "rows": rows,
    }


def us_v12_cost_model() -> CostModel:
    """The schedule ``COSTS_FLAG`` claims. A default-field change should fail tests."""
    return CostModel()


def _parse_symbols(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    return _us_symbols(raw.split(","))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description=(
            f"Score the US Top-50 strategy board (grid {GRID_VERSION}, "
            f"costs {COSTS_FLAG}) and store the top {TOP_N} per symbol."
        )
    )
    parser.add_argument("--db", type=Path, default=None, help="DuckDB file (default DIXON_DB)")
    parser.add_argument(
        "--as-of",
        default="",
        help="Session date YYYY-MM-DD (default: latest US bar). Window is the prior 12 months.",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated US tickers. Default is that session's US Top-50. A subset replaces only those symbols.",
    )
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    try:
        symbols = _parse_symbols(args.symbols)
        summary = run_board(db_path=args.db, as_of=as_of, symbols=symbols)
    except Exception:
        log.exception("strategy board failed")
        sys.exit(1)
    print(json.dumps(summary, sort_keys=True))
    sys.exit(0)


if __name__ == "__main__":
    main()

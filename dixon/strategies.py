"""Owner-private strategy CRUD. One symbol per strategy."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import duckdb

from dixon.rules import validate_rules
from dixon.screener import to_date
from dixon.yahoo import infer_market


def _row_to_strategy(row: tuple) -> dict[str, Any]:
    (
        sid,
        owner,
        name,
        symbol,
        market,
        timeframe,
        rules_json,
        created_at,
        updated_at,
    ) = row
    return {
        "id": sid,
        "owner_email": owner,
        "name": name,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "rules": json.loads(rules_json),
        "created_at": _ts(created_at),
        "updated_at": _ts(updated_at),
    }


def _ts(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(timespec="seconds")
        except TypeError:
            return to_date(value).isoformat()
    return str(value)


def _normalize_payload(payload: dict[str, Any]) -> tuple[str, str, str, str, str]:
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 32:
        raise ValueError("symbol is required")
    timeframe = str(payload.get("timeframe") or "daily").strip().lower()
    if timeframe not in {"daily", "weekly", "monthly"}:
        raise ValueError("timeframe must be daily, weekly, or monthly")
    market = infer_market(symbol)
    rules = validate_rules(payload.get("rules"))
    return name, symbol, market, timeframe, json.dumps(rules, separators=(",", ":"), sort_keys=True)


def create_strategy(
    con: duckdb.DuckDBPyConnection, owner_email: str, payload: dict[str, Any]
) -> dict[str, Any]:
    name, symbol, market, timeframe, rules_json = _normalize_payload(payload)
    sid = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO strategies (
          id, owner_email, name, symbol, market, timeframe, rules_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, now(), now())
        """,
        [sid, owner_email, name, symbol, market, timeframe, rules_json],
    )
    return get_strategy(con, owner_email, sid)  # type: ignore[return-value]


def list_strategies(con: duckdb.DuckDBPyConnection, owner_email: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT id, owner_email, name, symbol, market, timeframe, rules_json, created_at, updated_at
        FROM strategies
        WHERE owner_email = ?
        ORDER BY updated_at DESC, name
        """,
        [owner_email],
    ).fetchall()
    return [_row_to_strategy(r) for r in rows]


def get_strategy(
    con: duckdb.DuckDBPyConnection, owner_email: str, strategy_id: str
) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT id, owner_email, name, symbol, market, timeframe, rules_json, created_at, updated_at
        FROM strategies
        WHERE id = ? AND owner_email = ?
        """,
        [strategy_id, owner_email],
    ).fetchone()
    if row is None:
        return None
    return _row_to_strategy(row)


def update_strategy(
    con: duckdb.DuckDBPyConnection,
    owner_email: str,
    strategy_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if get_strategy(con, owner_email, strategy_id) is None:
        return None
    name, symbol, market, timeframe, rules_json = _normalize_payload(payload)
    con.execute(
        """
        UPDATE strategies
        SET name = ?, symbol = ?, market = ?, timeframe = ?, rules_json = ?, updated_at = now()
        WHERE id = ? AND owner_email = ?
        """,
        [name, symbol, market, timeframe, rules_json, strategy_id, owner_email],
    )
    return get_strategy(con, owner_email, strategy_id)


def delete_strategy(
    con: duckdb.DuckDBPyConnection, owner_email: str, strategy_id: str
) -> bool:
    existing = get_strategy(con, owner_email, strategy_id)
    if existing is None:
        return False
    con.execute(
        "DELETE FROM strategies WHERE id = ? AND owner_email = ?",
        [strategy_id, owner_email],
    )
    return True

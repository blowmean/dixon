"""DuckDB connection helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from dixon.config import DB_PATH, SCHEMA_SQL, DATA_DIR


def connect(db_path: Path | None = None, apply_schema: bool = True) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    if apply_schema:
        sql = SCHEMA_SQL.read_text()
        con.execute(sql)
    return con


def apply_schema_cli() -> None:
    p = argparse.ArgumentParser(description="Apply Dixon schema to DuckDB")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument(
        "--apply-schema",
        action="store_true",
        default=True,
        help="Apply schema.sql (default: true)",
    )
    args = p.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(args.db, apply_schema=True)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1"
        ).fetchall()
    ]
    con.close()
    print(f"OK schema applied db={args.db}")
    print("tables:", ", ".join(tables))


if __name__ == "__main__":
    apply_schema_cli()

"""Paths and runtime config for Project Dixon."""

from __future__ import annotations

import os
from pathlib import Path

# Package root = project-dixon/
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DIXON_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("DIXON_DB", DATA_DIR / "dixon.duckdb"))
PARQUET_ROOT = Path(os.environ.get("DIXON_PARQUET_ROOT", DATA_DIR / "parquet"))
SCHEMA_SQL = ROOT / "schema.sql"
UNIVERSE_DIR = ROOT / "universe"
GOLDEN_FILE = UNIVERSE_DIR / "golden.txt"
DAY1_FILE = UNIVERSE_DIR / "day1.txt"
ALL_FILE = UNIVERSE_DIR / "all.txt"
PHASE_A_FILE = UNIVERSE_DIR / "phase_a.txt"

# Yahoo rate limiting (seconds between ticker downloads)
YAHOO_SLEEP_SEC = float(os.environ.get("DIXON_YAHOO_SLEEP", "0.75"))
YAHOO_MAX_RETRIES = int(os.environ.get("DIXON_YAHOO_RETRIES", "3"))
HISTORY_YEARS = int(os.environ.get("DIXON_HISTORY_YEARS", "5"))

# Daily job: overwrite this many calendar days so soft Yahoo restates land
# (weekends/holidays included — typically covers ~7–10 sessions at 14 days)
RESTATE_DAYS = int(os.environ.get("DIXON_RESTATE_DAYS", "14"))

# API
API_HOST = os.environ.get("DIXON_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("DIXON_API_PORT", "8765"))
API_KEY = os.environ.get("DIXON_API_KEY", "")  # empty = no auth required

# Owner email for localhost and X-API-Key callers (no Access JWT).
# Browser users behind Cloudflare Access are attributed by the JWT email claim.
DEFAULT_OWNER = os.environ.get("DIXON_DEFAULT_OWNER", "paul.kf.leung@gmail.com").strip().lower()

# Exchange calendar names (exchange_calendars package)
CALENDAR_BY_MARKET = {
    "US": "XNYS",   # NYSE — covers US equities early closes
    "HK": "XHKG",   # HKEX — half-day sessions
}

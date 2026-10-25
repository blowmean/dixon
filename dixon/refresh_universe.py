"""Refresh universe listing files for all HK + US equities.

Writes:
  universe/us_all.txt
  universe/hk_all.txt
  universe/all.txt   (union + golden)

Usage:
  python -m dixon.refresh_universe
  python -m dixon.refresh_universe --skip-download   # rebuild from cached downloads
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
from datetime import date
from pathlib import Path

import httpx

from dixon.config import GOLDEN_FILE, UNIVERSE_DIR

log = logging.getLogger("dixon.refresh_universe")

NASDAQ_LISTED = "https://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt"
OTHER_LISTED = "https://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt"
# Fallbacks when ftp.nasdaqtrader.com is unreachable
NASDAQ_STOCK_SCREENER = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=25000&offset=0&download=true"
)
NASDAQ_ETF_SCREENER = (
    "https://api.nasdaq.com/api/screener/etf"
    "?tableonly=true&limit=25000&offset=0&download=true"
)
HKEX_LIST_XLSX = (
    "https://www.hkex.com.hk/eng/services/trading/securities/"
    "securitieslists/ListOfSecurities.xlsx"
)

CACHE_DIR = UNIVERSE_DIR / ".cache"
US_ALL = UNIVERSE_DIR / "us_all.txt"
HK_ALL = UNIVERSE_DIR / "hk_all.txt"
ALL_FILE = UNIVERSE_DIR / "all.txt"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# Name patterns to exclude from US lists
_US_EXCLUDE_NAME = re.compile(
    r"\b(Warrant|Warrants|Preferred|Preference|Right|Rights|"
    r"Unit(?!ed)|Units|Test\s+Issue|Test\s+Stock|"
    r"Depositary\s+Share)\b",
    re.I,
)
# Symbol patterns: $ preferreds, weird suffixes
_US_EXCLUDE_SYM = re.compile(r"[\$]|/")


def _download(url: str, dest: Path, *, timeout: float = 90.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("GET %s → %s", url, dest)
    with httpx.Client(headers=_UA, timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    log.info("  saved %d bytes", dest.stat().st_size)
    return dest


def _yahoo_us_symbol(raw: str) -> str | None:
    """Map NASDAQ directory symbol to Yahoo ticker (BRK.B → BRK-B)."""
    s = raw.strip().upper()
    if not s or _US_EXCLUDE_SYM.search(s):
        return None
    # Class shares: BRK.B / BF.A → BRK-B / BF-A
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and parts[0].isalnum() and parts[1].isalnum() and len(parts[1]) <= 2:
            s = f"{parts[0]}-{parts[1]}"
        else:
            return None
    if not re.fullmatch(r"[A-Z0-9\-]+", s):
        return None
    return s


def _keep_us_name(name: str) -> bool:
    return not bool(_US_EXCLUDE_NAME.search(name or ""))


def fetch_us_from_nasdaqtrader(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    """Primary US source: NASDAQ Trader Symbol Directory."""
    nasdaq_path = cache / "nasdaqlisted.txt"
    other_path = cache / "otherlisted.txt"
    if not skip_download or not nasdaq_path.exists() or not other_path.exists():
        _download(NASDAQ_LISTED, nasdaq_path, timeout=25.0)
        _download(OTHER_LISTED, other_path, timeout=25.0)

    symbols: set[str] = set()
    excluded = {"test": 0, "name": 0, "sym": 0}

    # nasdaqlisted: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    with nasdaq_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            if not sym or sym.startswith("File Creation"):
                continue
            if (row.get("Test Issue") or "").strip().upper() == "Y":
                excluded["test"] += 1
                continue
            name = row.get("Security Name") or ""
            if not _keep_us_name(name):
                excluded["name"] += 1
                continue
            y = _yahoo_us_symbol(sym)
            if y is None:
                excluded["sym"] += 1
                continue
            symbols.add(y)

    # otherlisted: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    with other_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            sym = (row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip()
            if not sym or sym.startswith("File Creation"):
                continue
            if (row.get("Test Issue") or "").strip().upper() == "Y":
                excluded["test"] += 1
                continue
            name = row.get("Security Name") or ""
            if not _keep_us_name(name):
                excluded["name"] += 1
                continue
            y = _yahoo_us_symbol(sym)
            if y is None:
                excluded["sym"] += 1
                continue
            symbols.add(y)

    note = (
        f"source=nasdaqtrader excluded test={excluded['test']} "
        f"name_filter={excluded['name']} sym_filter={excluded['sym']}"
    )
    return sorted(symbols), note


def fetch_us_from_screener(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    """Fallback US source: NASDAQ.com stock + ETF screeners."""
    stock_path = cache / "nasdaq_screener_stocks.json"
    etf_path = cache / "nasdaq_screener_etf.json"
    if not skip_download or not stock_path.exists():
        _download(NASDAQ_STOCK_SCREENER, stock_path, timeout=90.0)
    if not skip_download or not etf_path.exists():
        _download(NASDAQ_ETF_SCREENER, etf_path, timeout=90.0)

    import json

    symbols: set[str] = set()
    excluded = {"name": 0, "sym": 0}

    def _rows(path: Path) -> list[dict]:
        data = json.loads(path.read_text(encoding="utf-8"))
        d = data.get("data") or {}
        # stocks: data.rows ; etf: data.data.rows
        if isinstance(d.get("rows"), list):
            return d["rows"]
        inner = d.get("data") or {}
        if isinstance(inner, dict) and isinstance(inner.get("rows"), list):
            return inner["rows"]
        return []

    for path in (stock_path, etf_path):
        for row in _rows(path):
            sym = (row.get("symbol") or "").strip().upper()
            name = row.get("name") or row.get("companyName") or ""
            if not _keep_us_name(name):
                excluded["name"] += 1
                continue
            y = _yahoo_us_symbol(sym)
            if y is None:
                excluded["sym"] += 1
                continue
            symbols.add(y)

    note = (
        f"source=nasdaq_screener_fallback (ftp.nasdaqtrader unreachable) "
        f"excluded name_filter={excluded['name']} sym_filter={excluded['sym']}"
    )
    return sorted(symbols), note


def fetch_us(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    try:
        return fetch_us_from_nasdaqtrader(cache, skip_download=skip_download)
    except Exception as e:  # noqa: BLE001
        log.warning("NASDAQ Trader download failed (%s); using screener fallback", e)
        return fetch_us_from_screener(cache, skip_download=skip_download)


def _hk_yahoo(code: str) -> str | None:
    """HKEX 5-digit code → Yahoo ####.HK (min 4 digits)."""
    s = str(code).strip()
    if not s.isdigit():
        return None
    n = int(s)
    if n <= 0:
        return None
    # Yahoo: 0700.HK, 9988.HK, 83690.HK (RMB counters are 5-digit)
    body = f"{n:04d}" if n < 10000 else str(n)
    return f"{body}.HK"


def fetch_hk(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    """HKEX List of Securities → ordinary shares + REITs + equity ETFs."""
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise SystemExit(
            "openpyxl required for HKEX list parse; pip install openpyxl"
        ) from e

    xlsx = cache / "ListOfSecurities.xlsx"
    if not skip_download or not xlsx.exists():
        _download(HKEX_LIST_XLSX, xlsx, timeout=120.0)

    # Do NOT use read_only — HKEX file has a broken used-range (only ~5 rows).
    wb = load_workbook(xlsx, data_only=True)
    ws = wb.active
    data = list(ws.values)
    if len(data) < 4:
        raise RuntimeError(f"HKEX list too short ({len(data)} rows)")

    header = data[2]
    cols = {}
    for i, h in enumerate(header):
        if h is None:
            continue
        key = str(h).strip().split("\n")[0].strip()
        cols[key] = i

    code_i = cols["Stock Code"]
    cat_i = cols["Category"]
    sub_i = cols["Sub-Category"]
    rmb_i = cols.get("RMB Counter")

    # Collect RMB-counter codes pointed at by HKD listings — drop those duplicates
    rmb_dupes: set[str] = set()
    if rmb_i is not None:
        for row in data[3:]:
            if not row or row[code_i] is None:
                continue
            rmb = row[rmb_i]
            if rmb is not None and str(rmb).strip():
                rmb_dupes.add(str(rmb).strip().zfill(5))

    keep_equity_subs = {
        "Equity Securities (Main Board)",
        "Equity Securities (GEM)",
        "Investment Companies",
        "Depositary Receipts",
        "Trading Only Securities",
    }
    symbols: set[str] = set()
    counts = {
        "equity": 0,
        "reit": 0,
        "etf": 0,
        "skipped_rmb": 0,
        "skipped_li": 0,
        "skipped_other": 0,
    }

    for row in data[3:]:
        if not row or row[code_i] is None:
            continue
        code = str(row[code_i]).strip()
        cat = (row[cat_i] or "").strip()
        sub = (row[sub_i] or "").strip()

        if code in rmb_dupes or (len(code) == 5 and code.startswith("8") and code in rmb_dupes):
            counts["skipped_rmb"] += 1
            continue
        # Also skip standalone RMB counters that mirror an HKD stock (8xxxx pattern
        # when the HKD twin exists via rmb_dupes already). Extra: any code listed
        # purely as RMB counter target.
        if code.zfill(5) in rmb_dupes:
            counts["skipped_rmb"] += 1
            continue

        keep = False
        bucket = "skipped_other"
        if cat == "Equity" and sub in keep_equity_subs:
            keep, bucket = True, "equity"
        elif cat == "Real Estate Investment Trusts":
            keep, bucket = True, "reit"
        elif cat == "Exchange Traded Products" and sub == "Exchange Traded Funds":
            keep, bucket = True, "etf"
        elif cat == "Exchange Traded Products" and sub == "Leveraged and Inverse":
            counts["skipped_li"] += 1
            continue

        if not keep:
            counts["skipped_other"] += 1
            continue

        y = _hk_yahoo(code)
        if y:
            symbols.add(y)
            counts[bucket] += 1

    note = (
        f"source=HKEX ListOfSecurities.xlsx equity={counts['equity']} "
        f"reit={counts['reit']} etf={counts['etf']} "
        f"excluded rmb_dupes={counts['skipped_rmb']} "
        f"leveraged_inverse={counts['skipped_li']} "
        f"other_cats={counts['skipped_other']} "
        f"(warrants/CBBCs/debt/equity-warrants excluded via category)"
    )
    return sorted(symbols), note


def _load_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sym = line.split("#", 1)[0].strip().upper()
        if sym:
            out.append(sym)
    return out


def write_list(path: Path, symbols: list[str], header_lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(header_lines + symbols) + "\n"
    path.write_text(body, encoding="utf-8")
    log.info("Wrote %s (%d symbols)", path, len(symbols))


def refresh(*, skip_download: bool = False) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    us, us_note = fetch_us(CACHE_DIR, skip_download=skip_download)
    hk, hk_note = fetch_hk(CACHE_DIR, skip_download=skip_download)
    golden = _load_symbols(GOLDEN_FILE)

    write_list(
        US_ALL,
        us,
        [
            f"# US equities + ETFs — refreshed {today}",
            f"# count={len(us)}",
            f"# {us_note}",
            "# Excludes: test issues, warrants/rights/units/preferreds (by name), "
            "symbols with $ or /; class shares mapped . → - for Yahoo.",
        ],
    )
    write_list(
        HK_ALL,
        hk,
        [
            f"# HKEX equities + REITs + equity ETFs — refreshed {today}",
            f"# count={len(hk)} Yahoo form ####.HK",
            f"# {hk_note}",
            "# Excludes: derivative warrants, CBBCs, debt, equity warrants, "
            "leveraged/inverse ETPs, RMB counter duplicates.",
        ],
    )

    seen: set[str] = set()
    all_syms: list[str] = []
    for s in golden + us + hk:
        if s not in seen:
            seen.add(s)
            all_syms.append(s)

    write_list(
        ALL_FILE,
        all_syms,
        [
            f"# Project Dixon full equity universe — refreshed {today}",
            f"# count={len(all_syms)} (us={len(us)} hk={len(hk)} golden={len(golden)})",
            f"# US: {us_note}",
            f"# HK: {hk_note}",
            "# Union of us_all + hk_all + golden; deduped, golden first.",
        ],
    )

    return {
        "date": today,
        "us": len(us),
        "hk": len(hk),
        "golden": len(golden),
        "all": len(all_syms),
        "us_note": us_note,
        "hk_note": hk_note,
        "files": [str(US_ALL), str(HK_ALL), str(ALL_FILE)],
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Refresh Dixon HK+US universe listing files")
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse files under universe/.cache if present",
    )
    args = p.parse_args()
    out = refresh(skip_download=args.skip_download)
    print(out)


if __name__ == "__main__":
    main()

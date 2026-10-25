"""Build Phase A universe: HSI + HSTECH + S&P 500 + Nasdaq-100 + index ETFs + golden.

Writes:
  universe/phase_a.txt
  universe/phase_a_hsi.txt
  universe/phase_a_hstech.txt
  universe/phase_a_spx.txt
  universe/phase_a_ndx.txt
  universe/phase_a_etf.txt

Usage:
  python -m dixon.refresh_phase_a
  python -m dixon.refresh_phase_a --skip-download   # reuse universe/.cache/phase_a/
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

from dixon.config import GOLDEN_FILE, PHASE_A_FILE, UNIVERSE_DIR
from dixon.refresh_universe import _load_symbols, _yahoo_us_symbol, write_list

log = logging.getLogger("dixon.refresh_phase_a")

CACHE_DIR = UNIVERSE_DIR / ".cache" / "phase_a"

WIKI_SPX = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
WIKI_HSI = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
LONGBRIDGE_HSTECH = "https://longbridge.com/en/quote/HSTECH.HK/indexes"

# Index-tracking ETFs (Yahoo-listed). Keep lean; SPY already in golden.
PHASE_A_ETFS = [
    "SPY",       # SPDR S&P 500
    "VOO",       # Vanguard S&P 500
    "IVV",       # iShares Core S&P 500
    "QQQ",       # Invesco Nasdaq-100
    "QQQM",      # Invesco Nasdaq-100 (lower fee)
    "2800.HK",   # Tracker Fund of Hong Kong (HSI)
    "2828.HK",   # Hang Seng China Enterprises (HSCEI) — liquid HK proxy
    "3067.HK",   # iShares Hang Seng TECH
    "3033.HK",   # CSOP Hang Seng TECH (largest HSTECH ETF)
    "3032.HK",   # Hang Seng TECH Index ETF
]

# Curated HSTECH fallback (Longbridge / HSIL, as of 2026-10-23).
# Used when live scrape fails (SPA / geo blocks).
HSTECH_FALLBACK = [
    "00020", "00100", "00241", "00285", "00300", "00700", "00981", "00992",
    "01024", "01211", "01347", "01698", "01810", "02015", "02382", "02513",
    "03690", "06618", "06690", "09618", "09626", "09660", "09863", "09866",
    "09868", "09888", "09903", "09961", "09988", "09999",
]

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _download_text(url: str, dest: Path, *, skip_download: bool, timeout: float = 60.0) -> str:
    if skip_download and dest.exists():
        return dest.read_text(encoding="utf-8", errors="replace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("GET %s → %s", url, dest)
    with httpx.Client(headers=_UA, timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        dest.write_text(r.text, encoding="utf-8")
        return r.text


def _hk_yahoo_code(code: str | int) -> str | None:
    s = str(code).strip()
    m = re.search(r"(\d{1,5})", s)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    body = f"{n:04d}" if n < 10000 else str(n)
    return f"{body}.HK"


def fetch_spx(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    html = _download_text(WIKI_SPX, cache / "spx.html", skip_download=skip_download)
    tables = pd.read_html(StringIO(html))
    t = tables[0]
    if "Symbol" not in t.columns:
        raise RuntimeError(f"SPX table missing Symbol: {list(t.columns)}")
    syms: set[str] = set()
    for raw in t["Symbol"]:
        y = _yahoo_us_symbol(str(raw))
        if y:
            syms.add(y)
    return sorted(syms), f"source=wikipedia List_of_S&P_500_companies count={len(syms)}"


def fetch_ndx(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    html = _download_text(WIKI_NDX, cache / "ndx.html", skip_download=skip_download)
    tables = pd.read_html(StringIO(html))
    t = tables[0]
    col = "Ticker" if "Ticker" in t.columns else ("Symbol" if "Symbol" in t.columns else None)
    if col is None:
        raise RuntimeError(f"NDX table missing Ticker/Symbol: {list(t.columns)}")
    syms: set[str] = set()
    for raw in t[col]:
        y = _yahoo_us_symbol(str(raw))
        if y:
            syms.add(y)
    return sorted(syms), f"source=wikipedia List_of_NASDAQ-100_companies count={len(syms)}"


def fetch_hsi(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    html = _download_text(WIKI_HSI, cache / "hsi.html", skip_download=skip_download)
    tables = pd.read_html(StringIO(html))
    hsi_t = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if "Ticker" in cols and len(t) >= 50:
            hsi_t = t
            break
    if hsi_t is None:
        raise RuntimeError("HSI constituent table not found on Hang_Seng_Index")
    syms: set[str] = set()
    for raw in hsi_t["Ticker"]:
        y = _hk_yahoo_code(raw)
        if y:
            syms.add(y)
    return sorted(syms), f"source=wikipedia Hang_Seng_Index count={len(syms)}"


def fetch_hstech(cache: Path, *, skip_download: bool) -> tuple[list[str], str]:
    """Prefer Longbridge table; fall back to curated Oct-2026 list."""
    dest = cache / "hstech_longbridge.html"
    note_parts: list[str] = []
    try:
        html = _download_text(LONGBRIDGE_HSTECH, dest, skip_download=skip_download)
        # SPA may omit rows; also match static/SSR patterns
        codes = set(re.findall(r"\bHK\s+(\d{4,5})\b", html))
        codes |= set(re.findall(r"\b(\d{5})\.HK\b", html))
        # Name/code column style: "TENCENT HK 00700"
        codes |= set(re.findall(r"\bHK\s+(0\d{4})\b", html))
        if len(codes) >= 25:
            syms = sorted({_hk_yahoo_code(c) for c in codes if _hk_yahoo_code(c)})
            return syms, f"source=longbridge HSTECH.HK indexes count={len(syms)}"
        note_parts.append(f"longbridge_scrape_codes={len(codes)}")
    except Exception as e:  # noqa: BLE001
        note_parts.append(f"longbridge_error={e}")
        log.warning("HSTECH live scrape failed: %s", e)

    syms = sorted({_hk_yahoo_code(c) for c in HSTECH_FALLBACK if _hk_yahoo_code(c)})
    note = (
        f"source=curated_fallback_2026-10-23 (Longbridge/HSIL) count={len(syms)}"
        + (f" notes={';'.join(note_parts)}" if note_parts else "")
    )
    return syms, note


def refresh(*, skip_download: bool = False) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    spx, spx_note = fetch_spx(CACHE_DIR, skip_download=skip_download)
    ndx, ndx_note = fetch_ndx(CACHE_DIR, skip_download=skip_download)
    hsi, hsi_note = fetch_hsi(CACHE_DIR, skip_download=skip_download)
    hstech, hstech_note = fetch_hstech(CACHE_DIR, skip_download=skip_download)
    etfs = list(PHASE_A_ETFS)
    golden = _load_symbols(GOLDEN_FILE)

    write_list(
        UNIVERSE_DIR / "phase_a_spx.txt",
        spx,
        [f"# S&P 500 constituents — refreshed {today}", f"# {spx_note}"],
    )
    write_list(
        UNIVERSE_DIR / "phase_a_ndx.txt",
        ndx,
        [f"# Nasdaq-100 constituents — refreshed {today}", f"# {ndx_note}"],
    )
    write_list(
        UNIVERSE_DIR / "phase_a_hsi.txt",
        hsi,
        [f"# Hang Seng Index constituents — refreshed {today}", f"# {hsi_note}"],
    )
    write_list(
        UNIVERSE_DIR / "phase_a_hstech.txt",
        hstech,
        [f"# Hang Seng TECH constituents — refreshed {today}", f"# {hstech_note}"],
    )
    write_list(
        UNIVERSE_DIR / "phase_a_etf.txt",
        etfs,
        [
            f"# Phase A index ETFs — refreshed {today}",
            f"# count={len(etfs)} SPY/QQQ/HSI/HSTECH proxies",
        ],
    )

    seen: set[str] = set()
    all_syms: list[str] = []
    for s in golden + etfs + hsi + hstech + spx + ndx:
        if s not in seen:
            seen.add(s)
            all_syms.append(s)

    write_list(
        PHASE_A_FILE,
        all_syms,
        [
            f"# Project Dixon Phase A universe — refreshed {today}",
            f"# count={len(all_syms)} "
            f"(hsi={len(hsi)} hstech={len(hstech)} spx={len(spx)} ndx={len(ndx)} "
            f"etf={len(etfs)} golden={len(golden)}; deduped, golden+etf first)",
            f"# HSI: {hsi_note}",
            f"# HSTECH: {hstech_note}",
            f"# SPX: {spx_note}",
            f"# NDX: {ndx_note}",
            "# Locked scope: HSI + HSTECH + S&P 500 + Nasdaq-100 + index ETFs + golden.",
            "# Do NOT expand to universe/all.txt (~14k) without Paul approval.",
        ],
    )

    return {
        "date": today,
        "hsi": len(hsi),
        "hstech": len(hstech),
        "spx": len(spx),
        "ndx": len(ndx),
        "etf": len(etfs),
        "golden": len(golden),
        "phase_a": len(all_syms),
        "file": str(PHASE_A_FILE),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Refresh Dixon Phase A universe files")
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse files under universe/.cache/phase_a if present",
    )
    args = p.parse_args()
    out = refresh(skip_download=args.skip_download)
    print(out)


if __name__ == "__main__":
    main()

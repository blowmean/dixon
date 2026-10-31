"""US Top-50 overnight board: s1-v1 grid, US v1.2 costs, bit-stable golden."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dixon.api import app
from dixon.backtest import run_backtest
from dixon.db import connect
from dixon.rules import validate_rules
from dixon.strategy_board import (
    COSTS_FLAG,
    GRID_VERSION,
    TOP_N,
    grid_cells,
    main,
    rank_rows,
    read_board,
    rolling_window,
    run_board,
    us_v12_cost_model,
    write_board_rows,
)

FIXTURE = Path(__file__).parent / "fixtures" / "strategy_board_s1_v1_aapl.json"
GOLDEN_N = 122
GOLDEN_START = date(2024, 6, 3)
POOL_DAY = date(2024, 7, 1)

EMPTY_RULES = validate_rules(
    {
        "long": {"logic": "AND", "conditions": []},
        "flat": {"logic": "AND", "conditions": []},
        "short": {"logic": "AND", "conditions": []},
    }
)
EMPTY_RULES_JSON = json.dumps(EMPTY_RULES, separators=(",", ":"), sort_keys=True)

EXPECTED_IDS = (
    "rsi/p2/lte10",
    "rsi/p2/lte20",
    "rsi/p2/lte30",
    "rsi/p5/lte10",
    "rsi/p5/lte20",
    "rsi/p5/lte30",
    "rsi/p7/lte10",
    "rsi/p7/lte20",
    "rsi/p7/lte30",
    "rsi/p14/lte10",
    "rsi/p14/lte20",
    "rsi/p14/lte30",
    "rvol/k1.2/n10",
    "rvol/k1.2/n20",
    "rvol/k1.5/n10",
    "rvol/k1.5/n20",
    "rvol/k2.0/n10",
    "rvol/k2.0/n20",
    "donchian/n20/m10",
    "donchian/n55/m20",
    "anchor/relvol",
    "anchor/connors",
    "anchor/donchian",
    "anchor/roc7",
    "anchor/ema921",
)

_SEARCH_TYPES = {
    "rsi": {"rsi_lte", "rsi_gte", "price_gt_ma"},
    "rvol": {"rel_volume", "price_gt_ma", "price_lt_ma"},
    "donchian": {"donchian_high", "donchian_low"},
}


def golden_sessions(n: int = GOLDEN_N, start: date = GOLDEN_START) -> list[date]:
    day = start
    out: list[date] = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def golden_closes(n: int = GOLDEN_N) -> list[float]:
    """Flat warm-up, a grind higher, a two-day break, then a recovery.

    The grind is there so a channel/momentum cell can book a closed trade.
    The break is there so an RSI cell can see an oversold print.
    """
    px = 100.0
    closes: list[float] = []
    for i in range(n):
        if i < 40:
            px = 100.0
        elif i < 90:
            px *= 1.008
        elif i < 92:
            px *= 0.90
        else:
            px *= 1.01
        closes.append(px)
    return closes


def seed_golden(con, symbol: str = "AAPL") -> date:
    sessions = golden_sessions()
    closes = golden_closes()
    prev = closes[0]
    rows = []
    for i, (day, close) in enumerate(zip(sessions, closes)):
        open_ = prev if i else close
        high = max(open_, close) * 1.002
        low = min(open_, close) * 0.998
        volume = 4_000_000 if i % 7 == 0 else 1_000_000
        rows.append(("US", symbol, day, open_, high, low, close, volume))
        prev = close
    con.executemany(
        """
        INSERT INTO daily_bars
          (market, symbol, trade_date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return sessions[-1]


def _stored_row(
    symbol: str,
    as_of: date,
    rank: int,
    pnl: float,
    *,
    rule_id: str = "anchor/roc7",
    trades: int = 1,
) -> dict:
    return {
        "market": "US",
        "grid_version": GRID_VERSION,
        "as_of": as_of,
        "symbol": symbol,
        "rank": rank,
        "family": "anchor",
        "rule_id": rule_id,
        "rules_json": EMPTY_RULES_JSON,
        "params_json": '{"preset":"roc7"}',
        "summary": rule_id,
        "total_pnl": pnl,
        "trade_count": trades,
        "window_start": rolling_window(as_of)[0],
        "window_end": as_of,
        "costs_flag": COSTS_FLAG,
    }


def _scored(symbol, start, end, pnl, rule_id="anchor/roc7", trades=1):
    return {
        "symbol": symbol,
        "market": "US",
        "family": "anchor",
        "rule_id": rule_id,
        "summary": rule_id,
        "params": {"preset": "roc7"},
        "params_json": '{"preset":"roc7"}',
        "rules": EMPTY_RULES,
        "rules_json": EMPTY_RULES_JSON,
        "total_pnl": pnl,
        "trade_count": trades,
        "window_start": start,
        "window_end": end,
        "costs_flag": COSTS_FLAG,
    }


def _condition_types(rules: dict) -> set[str]:
    found: set[str] = set()
    for side in ("long", "flat", "short"):
        for cond in rules[side]["conditions"]:
            found.add(cond["type"])
    return found


def test_grid_catalog_is_frozen_s1_v1():
    cells = grid_cells()
    assert GRID_VERSION == "s1-v1"
    assert TOP_N == 3
    assert tuple(c.rule_id for c in cells) == EXPECTED_IDS
    by_id = {c.rule_id: c for c in cells}
    assert len(by_id) == 25
    for cell in cells:
        if cell.family == "anchor":
            continue
        assert _condition_types(cell.rules()) <= _SEARCH_TYPES[cell.family]
    for cell in cells:
        if cell.family != "rsi":
            continue
        rules = cell.rules()
        entry = rules["long"]["conditions"][0]
        assert entry["type"] == "rsi_lte"
        assert entry["period"] in {2, 5, 7, 14}
        assert entry["threshold"] in {10.0, 20.0, 30.0}
        flat = rules["flat"]
        assert flat["logic"] == "OR"
        assert flat["conditions"][0] == {"type": "price_gt_ma", "window": 5}
        exit_rsi = flat["conditions"][1]
        assert exit_rsi["type"] == "rsi_gte"
        assert exit_rsi["period"] == entry["period"]
        assert exit_rsi["threshold"] == 70.0
        assert rules["short"]["conditions"] == []
        assert "trail_pct" not in rules
    # Anchors that repeat a family cell stay in the pool as their own rule_id.
    same_donchian = {
        c.rule_id for c in cells if c.rules_json == by_id["anchor/donchian"].rules_json
    }
    same_relvol = {
        c.rule_id for c in cells if c.rules_json == by_id["anchor/relvol"].rules_json
    }
    assert same_donchian == {"anchor/donchian", "donchian/n20/m10"}
    assert same_relvol == {"anchor/relvol", "rvol/k1.5/n20"}
    connors = by_id["anchor/connors"].rules()
    assert connors["long"]["conditions"] == [
        {"type": "rsi_lte", "period": 2, "threshold": 5.0}
    ]
    assert connors["flat"]["logic"] == "OR"
    assert connors["flat"]["conditions"] == [
        {"type": "price_gt_ma", "window": 5},
        {"type": "rsi_gte", "period": 2, "threshold": 70.0},
    ]
    roc = by_id["anchor/roc7"].rules()
    assert roc["trail_pct"] == 5.0
    assert roc["long"]["conditions"] == [{"type": "roc_gte", "period": 7, "threshold": 1.0}]
    assert roc["short"]["conditions"] == [{"type": "roc_lte", "period": 7, "threshold": -1.0}]
    assert roc["flat"]["conditions"] == []
    ema = by_id["anchor/ema921"].rules()
    assert ema["long"]["conditions"] == [
        {"type": "ema_gt_ema", "fast": 9, "slow": 21},
        {"type": "rel_volume", "k": 1.5, "window": 20},
    ]
    assert ema["short"]["conditions"] == [
        {"type": "ema_lt_ema", "fast": 9, "slow": 21},
        {"type": "rel_volume", "k": 1.5, "window": 20},
    ]
    assert ema["flat"]["conditions"] == []
    assert "trail_pct" not in ema
    relvol = by_id["anchor/relvol"].rules()
    assert relvol["long"]["conditions"] == [
        {"type": "price_gt_ma", "window": 20},
        {"type": "rel_volume", "k": 1.5, "window": 20},
    ]
    assert relvol["flat"]["conditions"] == [{"type": "price_lt_ma", "window": 20}]


def test_rolling_window_is_twelve_calendar_months():
    assert rolling_window(date(2026, 10, 31)) == (date(2025, 10, 31), date(2026, 10, 31))
    assert rolling_window(date(2024, 2, 29)) == (date(2023, 2, 28), date(2024, 2, 29))
    assert rolling_window(date(2025, 2, 28)) == (date(2024, 2, 28), date(2025, 2, 28))


def test_costs_flag_matches_us_v12_defaults():
    model = us_v12_cost_model()
    assert COSTS_FLAG == "us-v1.2"
    assert model.us_commission_per_share == 0.005
    assert model.us_commission_min == 1.0
    assert model.us_spread_bps == 5.0
    assert model.equity_fraction == 0.20
    assert model.capital_usd == 100_000.0


def test_rank_tie_break_is_pnl_then_fewer_trades_then_rule_id():
    def row(rule_id, pnl, trades):
        return {"rule_id": rule_id, "total_pnl": pnl, "trade_count": trades}

    ranked = rank_rows(
        [
            row("b", 1.0, 2),
            row("a", 1.0, 2),
            row("c", 1.0, 1),
            row("d", 5.0, 9),
            row("e", -1.0, 0),
            row("f", 1.0, 2),
        ]
    )
    assert [r["rule_id"] for r in ranked] == ["d", "c", "a"]
    assert [r["rank"] for r in ranked] == [1, 2, 3]


def test_write_slice_semantics():
    con = connect()
    try:
        day_a = date(2024, 6, 3)
        day_b = date(2024, 6, 4)
        write_board_rows(
            con,
            [
                _stored_row("AAPL", day_a, 1, 1.0),
                _stored_row("MSFT", day_a, 1, 2.0, rule_id="anchor/ema921"),
            ],
        )
        write_board_rows(con, [_stored_row("AAPL", day_b, 1, 8.0)])
        write_board_rows(
            con,
            [_stored_row("AAPL", day_a, 1, 9.0, trades=4)],
            symbols=["AAPL"],
        )
        night_a = read_board(con, as_of=day_a)
        by_symbol = {row["symbol"]: row for row in night_a["rows"]}
        assert set(by_symbol) == {"AAPL", "MSFT"}
        assert by_symbol["AAPL"]["total_pnl"] == 9.0
        assert by_symbol["AAPL"]["trade_count"] == 4
        assert by_symbol["MSFT"]["total_pnl"] == 2.0
        assert read_board(con, as_of=day_b)["rows"][0]["total_pnl"] == 8.0

        write_board_rows(con, [_stored_row("MSFT", day_a, 1, 3.0, rule_id="anchor/ema921")])
        replaced = read_board(con, as_of=day_a)
        assert [row["symbol"] for row in replaced["rows"]] == ["MSFT"]
        assert replaced["rows"][0]["total_pnl"] == 3.0
        assert read_board(con, as_of=day_b)["count"] == 1

        write_board_rows(con, [_stored_row("MSFT", day_a, 1, 3.0, rule_id="anchor/ema921")])
        assert read_board(con, as_of=day_a)["rows"] == replaced["rows"]
    finally:
        con.close()


def test_rank_outside_top_3_is_rejected():
    con = connect()
    try:
        with pytest.raises(ValueError, match="rank"):
            write_board_rows(con, [_stored_row("AAPL", date(2024, 6, 3), 4, 1.0)])
    finally:
        con.close()


def _seed_top50_pool(con) -> None:
    rows = []
    for i in range(51):
        symbol = f"U{i:02d}"
        volume = 2_000 - i
        rows.append(("US", symbol, POOL_DAY, 10.0, 10.2, 9.8, 10.0, volume))
    rows.append(("HK", "0700.HK", POOL_DAY, 100.0, 101.0, 99.0, 100.0, 9_000_000))
    con.executemany(
        """
        INSERT INTO daily_bars
          (market, symbol, trade_date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def test_universe_is_that_sessions_us_top50(monkeypatch):
    seen: list[str] = []

    def fake(con, symbol, *, window_start, window_end, market="US"):
        seen.append(symbol)
        return [_scored(symbol, window_start, window_end, pnl=1.0)]

    monkeypatch.setattr("dixon.strategy_board.score_symbol", fake)
    con = connect()
    try:
        _seed_top50_pool(con)
        out = run_board(con, as_of=POOL_DAY)
        board = read_board(con, as_of=POOL_DAY)
    finally:
        con.close()
    expected = [f"U{i:02d}" for i in range(50)]
    assert out["status"] == "ok"
    assert out["universe"] == "us-top50"
    assert out["costs_flag"] == COSTS_FLAG
    assert out["symbol_count"] == 50
    assert out["row_count"] == 50
    assert seen == expected
    assert "U50" not in seen
    assert "0700.HK" not in seen
    assert board["count"] == 50
    assert {row["symbol"] for row in board["rows"]} == set(expected)


def test_symbol_override_replaces_only_those_names(monkeypatch):
    state = {"override": False}

    def fake(con, symbol, *, window_start, window_end, market="US"):
        pnl = 5.0 if state["override"] and symbol == "U00" else 1.0
        trades = 3 if state["override"] and symbol == "U00" else 1
        return [_scored(symbol, window_start, window_end, pnl=pnl, trades=trades)]

    monkeypatch.setattr("dixon.strategy_board.score_symbol", fake)
    con = connect()
    try:
        _seed_top50_pool(con)
        run_board(con, as_of=POOL_DAY)
        state["override"] = True
        out = run_board(con, as_of=POOL_DAY, symbols=["U00"])
        board = read_board(con, as_of=POOL_DAY)
    finally:
        con.close()
    assert out["universe"] == "symbols"
    assert out["symbol_count"] == 1
    assert board["symbol_count"] == 50
    by_symbol = {row["symbol"]: row for row in board["rows"]}
    assert by_symbol["U00"]["total_pnl"] == 5.0
    assert by_symbol["U00"]["trade_count"] == 3
    assert by_symbol["U01"]["total_pnl"] == 1.0


def test_non_us_symbol_is_rejected():
    con = connect()
    try:
        with pytest.raises(ValueError, match="US"):
            run_board(con, symbols=["0700.HK"])
    finally:
        con.close()


def test_empty_top50_clears_that_night_only():
    con = connect()
    try:
        stale = date(2024, 6, 3)
        kept = date(2024, 6, 4)
        write_board_rows(con, [_stored_row("AAPL", stale, 1, 1.0)])
        write_board_rows(con, [_stored_row("AAPL", kept, 1, 2.0)])
        out = run_board(con, as_of=stale)
        assert out["status"] == "empty_universe"
        assert out["symbol_count"] == 0
        cleared = read_board(con, as_of=stale)
        assert cleared["as_of"] == stale.isoformat()
        assert cleared["count"] == 0
        assert read_board(con, as_of=kept)["rows"][0]["total_pnl"] == 2.0
    finally:
        con.close()


def test_board_api_empty_auth_and_hk_rejected(monkeypatch):
    client = TestClient(app)
    empty = client.get("/strategy-board")
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["rows"] == []
    assert body["as_of"] is None
    assert body["grid_version"] == "s1-v1"
    assert body["universe"] == "us-top50"
    assert body["top_n"] == 3
    assert body["costs_flag"] is None

    missing = client.get("/strategy-board?as_of=2020-01-02")
    assert missing.status_code == 200
    assert missing.json()["as_of"] == "2020-01-02"
    assert missing.json()["count"] == 0

    assert client.get("/strategy-board?market=HK").status_code == 400
    assert client.get("/strategy-board?market=us").status_code == 200

    monkeypatch.setattr("dixon.api.API_KEY", "secret")
    public = {"Host": "dixon.robonet.cc"}
    assert client.get("/strategy-board", headers=public).status_code == 401
    keyed = client.get("/strategy-board", headers={**public, "X-API-Key": "secret"})
    assert keyed.status_code == 200
    local = client.get("/strategy-board", headers={"Host": "127.0.0.1:8765"})
    assert local.status_code == 200


def test_ui_exposes_the_board_tab():
    client = TestClient(app)
    page = client.get("/ui/")
    assert page.status_code == 200
    assert 'data-tab="board"' in page.text
    assert 'id="boardTable"' in page.text
    assert 'id="btnBoard"' in page.text
    assert "1y P&amp;L" in page.text or "1y P&L" in page.text
    js = client.get("/ui/app.js")
    assert js.status_code == 200
    text = js.text
    assert "/strategy-board" in text
    assert "function loadBoard" in text
    assert "function bindBoard" in text
    assert "function openBoardRow" in text
    boot = text.split("async function boot()", 1)[1].split("document.readyState", 1)[0]
    assert boot.index("bindBoard()") < boot.index("await loadChart()")
    assert boot.index("loadBoard()") < boot.index("await loadChart()")


def test_cli_help_and_empty_database(tmp_path, capsys):
    proc = subprocess.run(
        [sys.executable, "-m", "dixon.strategy_board", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "s1-v1" in proc.stdout
    db = tmp_path / "empty.duckdb"
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(db)])
    assert exc.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "no_session"
    assert summary["grid_version"] == "s1-v1"


def _roundtrip(payload):
    return json.loads(json.dumps(payload, sort_keys=True))


def test_golden_aapl_board_is_bit_stable_and_uses_us_costs():
    assert FIXTURE.is_file(), f"missing golden fixture {FIXTURE}"
    expected = json.loads(FIXTURE.read_text())
    con = connect()
    try:
        as_of = seed_golden(con)
        first = run_board(con, as_of=as_of)
        board = read_board(con, as_of=as_of)
        traded = next(row for row in board["rows"] if row["trade_count"] > 0)
        fresh = run_backtest(
            con,
            {
                "id": traded["rule_id"],
                "name": traded["summary"],
                "symbol": "AAPL",
                "market": "US",
                "timeframe": "daily",
                "rules": traded["rules"],
            },
            date.fromisoformat(traded["window_start"]),
            date.fromisoformat(traded["window_end"]),
            None,
        )
        zero = run_backtest(
            con,
            {
                "id": traded["rule_id"],
                "name": traded["summary"],
                "symbol": "AAPL",
                "market": "US",
                "timeframe": "daily",
                "rules": traded["rules"],
            },
            date.fromisoformat(traded["window_start"]),
            date.fromisoformat(traded["window_end"]),
            {
                "us_commission_per_share": 0.0,
                "us_commission_min": 0.0,
                "us_spread_bps": 0.0,
            },
        )
        second = run_board(con, as_of=as_of)
        again = read_board(con, as_of=as_of)
        client = TestClient(app)
        api_rows = client.get("/strategy-board").json()
    finally:
        con.close()

    assert first["status"] == "ok"
    assert first["grid_version"] == "s1-v1"
    assert first["costs_flag"] == "us-v1.2"
    assert first["universe"] == "us-top50"
    assert first["symbol_count"] == 1
    assert first["row_count"] == 3
    assert second["row_count"] == 3
    assert again == board
    assert [row["rank"] for row in board["rows"]] == [1, 2, 3]
    assert {row["symbol"] for row in board["rows"]} == {"AAPL"}
    assert all(row["costs_flag"] == "us-v1.2" for row in board["rows"])
    assert all(row["market"] == "US" for row in board["rows"])
    start, end = rolling_window(as_of)
    assert board["window_start"] == start.isoformat()
    assert board["window_end"] == end.isoformat()
    assert _roundtrip(board["rows"]) == expected["rows"]
    assert board["as_of"] == expected["as_of"]
    assert board["window_start"] == expected["window_start"]
    assert board["window_end"] == expected["window_end"]
    assert board["grid_version"] == expected["grid_version"]

    assert fresh["currency"] == "USD"
    assert fresh["starting_capital"] == 100_000
    assert fresh["costs"]["us_spread_bps"] == 5
    assert fresh["costs"]["us_commission_min"] == 1
    assert fresh["costs"]["us_commission_per_share"] == 0.005
    assert fresh["metrics"]["total_pnl"] == traded["total_pnl"]
    assert fresh["metrics"]["trade_count"] == traded["trade_count"]
    assert traded["trade_count"] > 0
    assert fresh["metrics"]["total_pnl"] != zero["metrics"]["total_pnl"]
    assert api_rows["count"] == 3
    assert _roundtrip(api_rows["rows"]) == expected["rows"]


def write_golden_fixture(path: Path | None = None) -> dict:
    """Regenerate tests/fixtures/strategy_board_s1_v1_aapl.json. Not used by pytest."""
    con = connect()
    try:
        as_of = seed_golden(con)
        run_board(con, as_of=as_of)
        board = read_board(con, as_of=as_of)
    finally:
        con.close()
    payload = {
        "grid_version": board["grid_version"],
        "costs_flag": board["costs_flag"],
        "symbol": "AAPL",
        "as_of": board["as_of"],
        "window_start": board["window_start"],
        "window_end": board["window_end"],
        "rows": _roundtrip(board["rows"]),
    }
    dest = path or FIXTURE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload

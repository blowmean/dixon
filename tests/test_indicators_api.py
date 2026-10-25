from datetime import date, timedelta

from fastapi.testclient import TestClient

from dixon.api import app
from dixon.compute_indicators import compute_named
from dixon.db import connect


def _client():
    return TestClient(app)


def _bars(symbol, market, closes, timeframe="daily"):
    con = connect()
    try:
        start = date(2024, 1, 2)
        for i, close in enumerate(closes):
            if timeframe == "daily":
                con.execute(
                    """
                    INSERT INTO daily_bars
                      (market, symbol, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1000)
                    """,
                    [market, symbol, start + timedelta(days=i), close, close, close, close],
                )
            else:
                period = start + timedelta(days=7 * i)
                con.execute(
                    """
                    INSERT INTO weekly_bars
                      (market, symbol, period_end, period_start, open, high, low, close, volume, bar_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1000, 5)
                    """,
                    [market, symbol, period, period, close, close, close, close],
                )
    finally:
        con.close()


def test_ondemand_ma_matches_formula_and_second_call_uses_cache(monkeypatch):
    _bars("AAPL", "US", [float(i) for i in range(1, 11)])
    calls = {"n": 0}
    real = compute_named

    def wrapped(close, name):
        calls["n"] += 1
        return real(close, name)

    monkeypatch.setattr("dixon.compute_indicators.compute_named", wrapped)
    client = _client()
    first = client.get("/indicators", params={"symbol": "AAPL", "name": "MA_5"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["count"] == 6  # bars 5..10
    assert abs(body["series"][-1]["value"] - 8.0) < 1e-9
    computed = calls["n"]
    assert computed >= 1
    second = client.get("/indicators", params={"symbol": "AAPL", "name": "MA_5"})
    assert second.status_code == 200
    assert calls["n"] == computed  # fingerprint hit: no recompute
    batch = client.get("/indicators", params={"symbol": "AAPL", "names": "MA_5,MA_9"})
    assert batch.status_code == 200
    assert "MA_9" in batch.json()["series"]


def test_weekly_ma_uses_weekly_closes():
    _bars("MSFT", "US", [1, 2, 3, 4], timeframe="weekly")
    client = _client()
    res = client.get(
        "/indicators",
        params={"symbol": "MSFT", "name": "MA_2", "timeframe": "weekly"},
    )
    assert res.status_code == 200, res.text
    series = res.json()["series"]
    assert abs(series[-1]["value"] - 3.5) < 1e-9
    stored = connect()
    try:
        names = [
            r[0]
            for r in stored.execute(
                "SELECT DISTINCT name FROM indicator_cache WHERE symbol = 'MSFT'"
            ).fetchall()
        ]
    finally:
        stored.close()
    assert "W|MA_2" in names
    assert "MA_2" not in names

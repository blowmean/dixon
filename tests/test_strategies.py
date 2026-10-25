import base64
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from dixon.api import app
from dixon.db import connect


def _jwt(email: str) -> str:
    def b64(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'email': email})}.sig"


def _bars():
    con = connect()
    try:
        start = date(2024, 1, 2)
        for i, close in enumerate([10, 10, 10, 10, 10, 12, 12, 12, 8, 8]):
            con.execute(
                """
                INSERT INTO daily_bars
                  (market, symbol, trade_date, open, high, low, close, volume)
                VALUES ('US', 'AAPL', ?, ?, ?, ?, ?, 1000000)
                """,
                [start + timedelta(days=i), close, close, close, close],
            )
    finally:
        con.close()


RULES = {
    "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 5}]},
    "flat": {"logic": "OR", "conditions": [{"type": "price_lt_ma", "window": 5}]},
    "short": {"logic": "AND", "conditions": []},
}


def test_strategies_are_private_to_the_access_email_and_backtest_runs():
    _bars()
    client = TestClient(app)
    paul = {"Cf-Access-Jwt-Assertion": _jwt("Paul.KF.Leung@gmail.com")}
    other = {"Cf-Access-Jwt-Assertion": _jwt("other@example.com")}

    created = client.post(
        "/strategies",
        headers=paul,
        json={"name": "AAPL SMA5", "symbol": "aapl", "timeframe": "daily", "rules": RULES},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["owner_email"] == "paul.kf.leung@gmail.com"
    assert body["symbol"] == "AAPL"
    sid = body["id"]

    assert client.get("/strategies", headers=other).json()["count"] == 0
    assert client.get(f"/strategies/{sid}", headers=other).status_code == 404
    assert client.delete(f"/strategies/{sid}", headers=other).status_code == 404

    # No JWT → default owner, who is the same person after normalisation.
    listed = client.get("/strategies")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["owner_email"] == "paul.kf.leung@gmail.com"

    bad = client.post(
        "/strategies",
        json={
            "name": "nope",
            "symbol": "AAPL",
            "rules": {
                "long": {"logic": "AND", "conditions": [{"type": "price_gt_ma", "window": 7}]},
                "flat": {"logic": "AND", "conditions": []},
                "short": {"logic": "AND", "conditions": []},
            },
        },
    )
    assert bad.status_code == 400

    run = client.post(
        f"/strategies/{sid}/backtest",
        json={"start": "2024-01-02", "end": "2024-01-11"},
    )
    assert run.status_code == 200, run.text
    metrics = run.json()["metrics"]
    assert metrics["trade_count"] == 1
    assert "win_rate_target" in metrics
    assert "max_drawdown_target" in metrics
    assert run.json()["currency"] == "USD"
    assert run.json()["starting_capital"] == 100_000

    updated = client.put(
        f"/strategies/{sid}",
        headers=paul,
        json={
            "name": "AAPL renamed",
            "symbol": "AAPL",
            "timeframe": "daily",
            "rules": RULES,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "AAPL renamed"
    assert client.delete(f"/strategies/{sid}", headers=paul).status_code == 204
    assert client.get(f"/strategies/{sid}", headers=paul).status_code == 404


def test_api_key_gate_and_localhost_default_owner(monkeypatch):
    monkeypatch.setattr("dixon.api.API_KEY", "secret")
    client = TestClient(app)
    blocked = client.get("/strategies", headers={"Host": "dixon.robonet.cc"})
    assert blocked.status_code == 401
    local = client.get("/strategies", headers={"Host": "127.0.0.1"})
    assert local.status_code == 200
    assert local.json()["owner_email"] == "paul.kf.leung@gmail.com"
    keyed = client.get(
        "/strategies",
        headers={"Host": "dixon.robonet.cc", "X-API-Key": "secret"},
    )
    assert keyed.status_code == 200
    assert keyed.json()["owner_email"] == "paul.kf.leung@gmail.com"
    missing_email = client.get(
        "/strategies",
        headers={
            "Host": "dixon.robonet.cc",
            "Cf-Access-Jwt-Assertion": _jwt_without_email(),
        },
    )
    assert missing_email.status_code == 401


def _jwt_without_email() -> str:
    def b64(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'sub': 'abc'})}.sig"

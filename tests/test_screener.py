from datetime import date, timedelta

from dixon.db import connect
from dixon.screener import build_top50, is_spike, spike_dates


def _seed(con, market, symbol, closes, highs, volumes):
    start = date(2024, 1, 2)
    for i, (close, high, volume) in enumerate(zip(closes, highs, volumes)):
        con.execute(
            """
            INSERT INTO daily_bars
              (market, symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [market, symbol, start + timedelta(days=i), close, high, close * 0.99, close, volume],
        )


def test_top50_is_per_market_and_spike_stays_inside_the_pool():
    con = connect()
    try:
        flat = [10.0] * 31
        highs = [10.5] * 31
        # 49 large quiet names, one inside-pool spike, one spike that misses the pool.
        for i in range(49):
            _seed(con, "US", f"F{i:03d}", flat, highs, [100_000_000] * 31)
        spike_close = flat[:-1] + [20.0]
        spike_high = highs[:-1] + [20.0]
        _seed(con, "US", "SPIKE_IN", spike_close, spike_high, [100_000] * 30 + [5_000_000])
        _seed(con, "US", "SPIKE_OUT", spike_close, spike_high, [10] * 31)
        # A loud HK name must not leak into the US pool.
        _seed(con, "HK", "0700.HK", spike_close, spike_high, [9_000_000_000] * 31)

        us = build_top50(con, "US", date(2024, 1, 2) + timedelta(days=30))
        assert us["count"] == 50
        symbols = [r["symbol"] for r in us["rows"]]
        assert "SPIKE_OUT" not in symbols
        assert "0700.HK" not in symbols
        assert symbols[-1] == "SPIKE_IN"
        flagged = [r["symbol"] for r in us["rows"] if r["spike"]]
        assert flagged == ["SPIKE_IN"]
        insider = us["rows"][-1]
        assert insider["day_gain"] >= 0.05
        assert insider["close"] > insider["prior_5d_high"]
        assert insider["sma30"] > insider["sma30_prev"]

        hk = build_top50(con, "HK", date(2024, 1, 2) + timedelta(days=30))
        assert [r["symbol"] for r in hk["rows"]] == ["0700.HK"]
        assert hk["rows"][0]["spike"] is True

        hits = spike_dates(
            con, "US", "SPIKE_IN", date(2024, 1, 2), date(2024, 1, 2) + timedelta(days=30)
        )
        assert date(2024, 1, 2) + timedelta(days=30) in hits
        missed = spike_dates(
            con, "US", "SPIKE_OUT", date(2024, 1, 2), date(2024, 1, 2) + timedelta(days=30)
        )
        assert missed == set()
    finally:
        con.close()


def test_flat_sma_is_not_a_spike():
    assert is_spike(
        {
            "close": 10,
            "prior_5d_high": 9,
            "n_prior5": 5,
            "prev_close": 9,
            "sma30": 10,
            "sma30_prev": 10,
            "n_sma30": 30,
            "n_sma30_prev": 30,
        }
    ) is False

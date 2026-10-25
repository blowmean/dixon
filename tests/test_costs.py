from dixon.costs import commission, hkex_tick, slip_per_share, CostModel


def test_hkex_tick_band_edges():
    assert hkex_tick(0.25) == 0.001
    assert hkex_tick(0.2501) == 0.005
    assert hkex_tick(0.50) == 0.005
    assert hkex_tick(10) == 0.01
    assert hkex_tick(10.01) == 0.02
    assert hkex_tick(100) == 0.05
    assert hkex_tick(100.01) == 0.10
    assert hkex_tick(5000) == 2.0
    assert hkex_tick(5000.01) == 5.0
    assert hkex_tick(9995) == 5.0


def test_default_slippage_and_commission():
    costs = CostModel()
    # Spread width is 2 ticks, so each side pays 1 tick. At 100 HKD the tick is 0.05.
    assert hkex_tick(100) == 0.05
    assert abs(slip_per_share("HK", 100, costs) - 0.05) < 1e-12
    assert abs(commission("HK", 1000, 100, costs) - 250.0) < 1e-9  # 25 bps of 100_000
    # US: 5 bps of price each side; $0.005/share with a $1 minimum.
    assert abs(slip_per_share("US", 100, costs) - 0.05) < 1e-12
    assert abs(commission("US", 10, 100, costs) - 1.0) < 1e-12
    assert abs(commission("US", 1000, 100, costs) - 5.0) < 1e-12

"""Transaction cost model for Dixon backtests (PRD v1.2).

HK bid-ask is a spread of `hk_spread_ticks` (default 2) on the HKEX price-band
tick table. Each fill crosses half of that spread (1 tick per side at the default).
US bid-ask is `us_spread_bps` (default 5) of price on each side.

Commission is charged on each fill: HK `hk_commission_bps` of notional (default 25);
US `max(us_commission_min, us_commission_per_share * shares)` (default $1 min, $0.005/share).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

# HKEX securities spread table (Part A). Upper bound is inclusive:
# 0.01–0.25 → 0.001, over 0.25–0.50 → 0.005, and so on.
_HKEX_BANDS: tuple[tuple[float, float], ...] = (
    (0.25, 0.001),
    (0.50, 0.005),
    (10.0, 0.01),
    (20.0, 0.02),
    (100.0, 0.05),
    (200.0, 0.10),
    (500.0, 0.20),
    (1000.0, 0.50),
    (2000.0, 1.0),
    (5000.0, 2.0),
    (9995.0, 5.0),
)


def hkex_tick(price: float) -> float:
    """Minimum price increment for an HKEX security at `price` (HKD)."""
    if price <= 0:
        raise ValueError("price must be positive")
    for upper, tick in _HKEX_BANDS:
        if price <= upper + 1e-9:
            return tick
    return 5.0


@dataclass
class CostModel:
    hk_commission_bps: float = 25.0
    hk_spread_ticks: float = 2.0
    us_commission_per_share: float = 0.005
    us_commission_min: float = 1.0
    us_spread_bps: float = 5.0
    equity_fraction: float = 0.20
    capital_hkd: float = 500_000.0
    capital_usd: float = 100_000.0

    def capital_for(self, market: str) -> float:
        if market == "HK":
            return float(self.capital_hkd)
        if market == "US":
            return float(self.capital_usd)
        raise ValueError(f"unknown market {market}")

    def currency_for(self, market: str) -> str:
        return "HKD" if market == "HK" else "USD"

    def to_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}


def cost_model_from_overrides(overrides: dict | None) -> CostModel:
    """Merge a partial override onto PRD defaults. Unknown keys are rejected."""
    base = CostModel()
    if not overrides:
        return base
    allowed = {f.name for f in fields(CostModel)}
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise ValueError(f"unknown cost fields: {', '.join(unknown)}")
    data = asdict(base)
    for key, value in overrides.items():
        if value is None:
            continue
        data[key] = float(value)
    model = CostModel(**data)
    if not 0 < model.equity_fraction <= 1:
        raise ValueError("equity_fraction must be in (0, 1]")
    if model.capital_hkd <= 0 or model.capital_usd <= 0:
        raise ValueError("capital must be positive")
    for name in (
        "hk_commission_bps",
        "hk_spread_ticks",
        "us_commission_per_share",
        "us_commission_min",
        "us_spread_bps",
    ):
        if getattr(model, name) < 0:
            raise ValueError(f"{name} must be >= 0")
    return model


def slip_per_share(market: str, price: float, costs: CostModel) -> float:
    """Adverse price move (cash per share) paid to cross the spread on one fill."""
    if price <= 0:
        return 0.0
    if market == "HK":
        return (costs.hk_spread_ticks / 2.0) * hkex_tick(price)
    if market == "US":
        return price * (costs.us_spread_bps / 10_000.0)
    raise ValueError(f"unknown market {market}")


def commission(market: str, shares: float, price: float, costs: CostModel) -> float:
    """One-side commission in local currency."""
    qty = abs(float(shares))
    if qty == 0 or price <= 0:
        return 0.0
    if market == "HK":
        return qty * price * (costs.hk_commission_bps / 10_000.0)
    if market == "US":
        return max(costs.us_commission_min, costs.us_commission_per_share * qty)
    raise ValueError(f"unknown market {market}")

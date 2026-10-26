"""Fixed-menu strategy rules (PRD v1.2) plus single-name signal predicates.

Condition types:
  price_gt_ma, price_lt_ma     close vs SMA(window) on the fixed MA menu
  ma_slope_gt_0, ma_slope_lt_0 SMA(window)_t vs SMA(window)_{t-1}
  ma_stack                     ordered windows, direction up (5>20>50) or down
  top50_spike                  symbol is inside that session's market Top-50
                               and the daily spike rule is true
  rel_volume                   volume >= k * SMA(volume, window); defaults k=1.5, window=20
  rsi_lte, rsi_gte             Wilder RSI(period) <= or >= threshold
                               defaults period=2, threshold 5 (lte) or 70 (gte)
  donchian_high                close > max high of the previous `window` bars (default 20)
  donchian_low                 close < min low of the previous `window` bars (default 10)

Sides are long / flat / short, each an AND or OR of conditions.
Empty side never fires. Priority when several sides fire on the same bar:
  long and short together → flat (conflict);
  else long;
  else short;
  else flat-rule;
  else hold the current side.

A flat exit therefore only acts when the long and short groups are false.
Donchian high and low breaks are mutually exclusive for overlapping lookbacks,
so a 20-bar high entry and a 10-bar low exit can share one bar's decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MA_MENU: tuple[int, ...] = (5, 9, 20, 30, 50, 100, 250)
MA_MENU_SET = set(MA_MENU)

REL_VOLUME_K_DEFAULT = 1.5
REL_VOLUME_WINDOW_DEFAULT = 20
RSI_PERIOD_DEFAULT = 2
RSI_LTE_THRESHOLD_DEFAULT = 5.0
RSI_GTE_THRESHOLD_DEFAULT = 70.0
DONCHIAN_HIGH_DEFAULT = 20
DONCHIAN_LOW_DEFAULT = 10

_LOOKBACK_MIN = 1
_LOOKBACK_MAX = 500
_RSI_PERIOD_MIN = 1
_RSI_PERIOD_MAX = 250
_K_MAX = 100.0

_WINDOWED = {"price_gt_ma", "price_lt_ma", "ma_slope_gt_0", "ma_slope_lt_0"}
_TYPES = _WINDOWED | {
    "ma_stack",
    "top50_spike",
    "rel_volume",
    "rsi_lte",
    "rsi_gte",
    "donchian_high",
    "donchian_low",
}
_SIDES = ("long", "flat", "short")


def _as_window(value: Any, label: str) -> int:
    try:
        window = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer MA window") from exc
    if isinstance(value, bool) or (isinstance(value, float) and not float(value).is_integer()):
        raise ValueError(f"{label} must be an integer MA window")
    if window not in MA_MENU_SET:
        allowed = ", ".join(str(w) for w in MA_MENU)
        raise ValueError(f"{label} {window} is not in the fixed menu ({allowed})")
    return window


def _as_int_in(value: Any, label: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{label} must be an integer")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < lo or value > hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return value


def _as_float_in(
    value: Any, label: str, lo: float, hi: float, *, lo_exclusive: bool = False
) -> float:
    if isinstance(value, bool) or isinstance(value, str) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if lo_exclusive:
        ok = lo < number <= hi
        bound = f"greater than {lo} and at most {hi}"
    else:
        ok = lo <= number <= hi
        bound = f"between {lo} and {hi}"
    if not ok:
        raise ValueError(f"{label} must be {bound}")
    return number


def _present(cond: dict[str, Any], key: str, default: Any) -> Any:
    if key not in cond or cond[key] is None:
        return default
    return cond[key]


def validate_rules(rules: Any) -> dict[str, Any]:
    """Return a canonical rule tree or raise ValueError."""
    if not isinstance(rules, dict):
        raise ValueError("rules must be an object with long, flat, and short groups")
    out: dict[str, Any] = {}
    for side in _SIDES:
        group = rules.get(side) or {}
        if not isinstance(group, dict):
            raise ValueError(f"{side} must be an object")
        logic = str(group.get("logic") or "AND").upper()
        if logic not in {"AND", "OR"}:
            raise ValueError(f"{side}.logic must be AND or OR")
        raw_conds = group.get("conditions") or []
        if not isinstance(raw_conds, list):
            raise ValueError(f"{side}.conditions must be a list")
        conds: list[dict[str, Any]] = []
        for i, cond in enumerate(raw_conds):
            if not isinstance(cond, dict):
                raise ValueError(f"{side}.conditions[{i}] must be an object")
            ctype = cond.get("type")
            if ctype not in _TYPES:
                raise ValueError(f"unknown condition type {ctype!r}")
            item: dict[str, Any] = {"type": ctype}
            if ctype in _WINDOWED:
                item["window"] = _as_window(cond.get("window"), f"{side} {ctype} window")
            elif ctype == "ma_stack":
                windows_raw = cond.get("windows")
                if not isinstance(windows_raw, list) or len(windows_raw) < 2:
                    raise ValueError("ma_stack needs at least two windows")
                windows = [
                    _as_window(w, f"ma_stack window[{j}]") for j, w in enumerate(windows_raw)
                ]
                direction = str(cond.get("direction") or "up").lower()
                if direction not in {"up", "down"}:
                    raise ValueError("ma_stack direction must be up or down")
                item["windows"] = windows
                item["direction"] = direction
            elif ctype == "rel_volume":
                item["k"] = _as_float_in(
                    _present(cond, "k", REL_VOLUME_K_DEFAULT),
                    f"{side} rel_volume k",
                    0.0,
                    _K_MAX,
                    lo_exclusive=True,
                )
                item["window"] = _as_int_in(
                    _present(cond, "window", REL_VOLUME_WINDOW_DEFAULT),
                    f"{side} rel_volume window",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
            elif ctype in {"rsi_lte", "rsi_gte"}:
                default_threshold = (
                    RSI_LTE_THRESHOLD_DEFAULT if ctype == "rsi_lte" else RSI_GTE_THRESHOLD_DEFAULT
                )
                item["period"] = _as_int_in(
                    _present(cond, "period", RSI_PERIOD_DEFAULT),
                    f"{side} {ctype} period",
                    _RSI_PERIOD_MIN,
                    _RSI_PERIOD_MAX,
                )
                item["threshold"] = _as_float_in(
                    _present(cond, "threshold", default_threshold),
                    f"{side} {ctype} threshold",
                    0.0,
                    100.0,
                )
            elif ctype == "donchian_high":
                item["window"] = _as_int_in(
                    _present(cond, "window", DONCHIAN_HIGH_DEFAULT),
                    f"{side} donchian_high window",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
            elif ctype == "donchian_low":
                item["window"] = _as_int_in(
                    _present(cond, "window", DONCHIAN_LOW_DEFAULT),
                    f"{side} donchian_low window",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
            conds.append(item)
        out[side] = {"logic": logic, "conditions": conds}
    return out


def windows_used(rules: dict[str, Any]) -> list[int]:
    """Price-SMA windows. Volume and Donchian lookbacks are not MA menu windows."""
    found: set[int] = set()
    for side in _SIDES:
        for cond in rules.get(side, {}).get("conditions", []):
            ctype = cond.get("type")
            if ctype in _WINDOWED and "window" in cond:
                found.add(int(cond["window"]))
            if ctype == "ma_stack":
                for w in cond.get("windows") or []:
                    found.add(int(w))
    return sorted(found)


def signal_requirements(rules: dict[str, Any]) -> dict[str, Any]:
    """Series the backtest must build before evaluating `rules`."""
    rsi: set[int] = set()
    volume: set[int] = set()
    high: set[int] = set()
    low: set[int] = set()
    needs_spike = False
    for side in _SIDES:
        for cond in rules.get(side, {}).get("conditions", []):
            ctype = cond.get("type")
            if ctype in {"rsi_lte", "rsi_gte"}:
                rsi.add(int(cond["period"]))
            elif ctype == "rel_volume":
                volume.add(int(cond["window"]))
            elif ctype == "donchian_high":
                high.add(int(cond["window"]))
            elif ctype == "donchian_low":
                low.add(int(cond["window"]))
            elif ctype == "top50_spike":
                needs_spike = True
    return {
        "rsi_periods": sorted(rsi),
        "volume_windows": sorted(volume),
        "donchian_high_windows": sorted(high),
        "donchian_low_windows": sorted(low),
        "needs_spike": needs_spike,
    }


@dataclass
class SignalView:
    """One bar of inputs for the rule tree. Missing series values are None."""

    close: float
    mas: dict[int, float | None]
    mas_prev: dict[int, float | None]
    spike: bool = False
    volume: float | None = None
    vol_sma: dict[int, float | None] = field(default_factory=dict)
    rsi: dict[int, float | None] = field(default_factory=dict)
    prior_high: dict[int, float | None] = field(default_factory=dict)
    prior_low: dict[int, float | None] = field(default_factory=dict)


def condition_true(cond: dict[str, Any], view: SignalView) -> bool:
    ctype = cond["type"]
    if ctype == "top50_spike":
        return bool(view.spike)
    if ctype == "price_gt_ma":
        ma = view.mas.get(cond["window"])
        return ma is not None and view.close > ma
    if ctype == "price_lt_ma":
        ma = view.mas.get(cond["window"])
        return ma is not None and view.close < ma
    if ctype == "ma_slope_gt_0":
        cur = view.mas.get(cond["window"])
        prev = view.mas_prev.get(cond["window"])
        return cur is not None and prev is not None and cur > prev
    if ctype == "ma_slope_lt_0":
        cur = view.mas.get(cond["window"])
        prev = view.mas_prev.get(cond["window"])
        return cur is not None and prev is not None and cur < prev
    if ctype == "ma_stack":
        vals = [view.mas.get(w) for w in cond["windows"]]
        if any(v is None for v in vals):
            return False
        if cond.get("direction", "up") == "down":
            return all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))  # type: ignore[operator]
        return all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))  # type: ignore[operator]
    if ctype == "rel_volume":
        vol = view.volume
        avg = view.vol_sma.get(cond["window"])
        if vol is None or avg is None or avg <= 0:
            return False
        # 1e-6 share absorbs binary dust when k * mean lands on the volume.
        return vol >= float(cond["k"]) * avg - 1e-6
    if ctype in {"rsi_lte", "rsi_gte"}:
        value = view.rsi.get(int(cond["period"]))
        if value is None:
            return False
        if ctype == "rsi_lte":
            return value <= float(cond["threshold"])
        return value >= float(cond["threshold"])
    if ctype == "donchian_high":
        level = view.prior_high.get(cond["window"])
        return level is not None and view.close > level
    if ctype == "donchian_low":
        level = view.prior_low.get(cond["window"])
        return level is not None and view.close < level
    return False


def _group_true(group: dict[str, Any], view: SignalView) -> bool:
    conds = group.get("conditions") or []
    if not conds:
        return False
    flags = [condition_true(c, view) for c in conds]
    if group.get("logic") == "OR":
        return any(flags)
    return all(flags)


def target_side(rules: dict[str, Any], view: SignalView, current: str) -> str:
    """Desired side after this bar's close. `current` is held when nothing fires."""
    long_on = _group_true(rules["long"], view)
    short_on = _group_true(rules["short"], view)
    flat_on = _group_true(rules["flat"], view)
    if long_on and short_on:
        return "flat"
    if long_on:
        return "long"
    if short_on:
        return "short"
    if flat_on:
        return "flat"
    return current

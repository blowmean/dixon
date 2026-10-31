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
  roc_gte, roc_lte             ROC(period) >= or <= threshold, in percent points
                               ROC = (close / close_period_bars_ago − 1) × 100
                               defaults period=7, threshold +1 (gte) or −1 (lte)
  ema_gt_ema, ema_lt_ema       EMA(fast) > EMA(slow), or EMA(fast) < EMA(slow)
                               defaults fast=9, slow=21. Spans are any integer
                               from 1 to 500, not the SMA menu (21 is allowed).
                               True on every bar the inequality holds (the stack),
                               not only the bar it crosses. Equal values and
                               warm-up are false. Volume is not inside this
                               predicate: AND a rel_volume condition to confirm.

Optional top-level `trail_pct` (percent points, not a fraction): while a
position is open, a close that has retraced that percent from the peak high
(long) or trough low (short) since the fill forces flat. Omit it for no stop.
The public ROC(7) pattern fills 5, meaning a 5% trail. The stop is not a
condition, so it still fires when the entry predicate remains true.

Sides are long / flat / short, each an AND or OR of conditions.
Empty side never fires. Priority when several sides fire on the same bar:
  long and short together → flat (conflict);
  else long;
  else short;
  else flat-rule;
  else hold the current side.

A flat exit therefore only acts when the long and short groups are false.
`trail_pct` is applied after that priority: it forces flat only when the
decision would keep the held side (still-true entry, or hold). An opposite
side on the same close still reverses.
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
ROC_PERIOD_DEFAULT = 7
ROC_GTE_THRESHOLD_DEFAULT = 1.0
ROC_LTE_THRESHOLD_DEFAULT = -1.0
# Public EMA 9/21 pattern. Volume confirm is a separate rel_volume condition
# (the preset uses the existing k=1.5, window=20), not a field on these types.
EMA_FAST_DEFAULT = 9
EMA_SLOW_DEFAULT = 21
# Filled by the ROC(7) public pattern. Omitted `trail_pct` means no stop.
K2_TRAIL_PCT_DEFAULT = 5.0
TRAIL_PCT_MAX = 100.0
ROC_THRESHOLD_MIN = -100.0
ROC_THRESHOLD_MAX = 10000.0

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
    "roc_gte",
    "roc_lte",
    "ema_gt_ema",
    "ema_lt_ema",
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
            elif ctype in {"roc_gte", "roc_lte"}:
                default_threshold = (
                    ROC_GTE_THRESHOLD_DEFAULT if ctype == "roc_gte" else ROC_LTE_THRESHOLD_DEFAULT
                )
                item["period"] = _as_int_in(
                    _present(cond, "period", ROC_PERIOD_DEFAULT),
                    f"{side} {ctype} period",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
                item["threshold"] = _as_float_in(
                    _present(cond, "threshold", default_threshold),
                    f"{side} {ctype} threshold",
                    ROC_THRESHOLD_MIN,
                    ROC_THRESHOLD_MAX,
                )
            elif ctype in {"ema_gt_ema", "ema_lt_ema"}:
                fast = _as_int_in(
                    _present(cond, "fast", EMA_FAST_DEFAULT),
                    f"{side} {ctype} fast",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
                slow = _as_int_in(
                    _present(cond, "slow", EMA_SLOW_DEFAULT),
                    f"{side} {ctype} slow",
                    _LOOKBACK_MIN,
                    _LOOKBACK_MAX,
                )
                if fast == slow:
                    raise ValueError(f"{side} {ctype} fast and slow must be different spans")
                item["fast"] = fast
                item["slow"] = slow
            conds.append(item)
        out[side] = {"logic": logic, "conditions": conds}
    trail = _canonical_trail(rules)
    if trail is not None:
        out["trail_pct"] = trail
    return out


def _canonical_trail(rules: dict[str, Any]) -> float | None:
    """Percent-point trail, or None when the stop is off.

    Missing and null both mean off, so older rules stay byte-for-byte canonical.
    """
    if "trail_pct" not in rules or rules["trail_pct"] is None:
        return None
    return _as_float_in(
        rules["trail_pct"],
        "trail_pct",
        0.0,
        TRAIL_PCT_MAX,
        lo_exclusive=True,
    )


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
    roc: set[int] = set()
    ema: set[int] = set()
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
            elif ctype in {"roc_gte", "roc_lte"}:
                roc.add(int(cond["period"]))
            elif ctype in {"ema_gt_ema", "ema_lt_ema"}:
                ema.add(int(cond["fast"]))
                ema.add(int(cond["slow"]))
            elif ctype == "top50_spike":
                needs_spike = True
    return {
        "rsi_periods": sorted(rsi),
        "volume_windows": sorted(volume),
        "donchian_high_windows": sorted(high),
        "donchian_low_windows": sorted(low),
        "roc_periods": sorted(roc),
        "ema_spans": sorted(ema),
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
    roc: dict[int, float | None] = field(default_factory=dict)
    emas: dict[int, float | None] = field(default_factory=dict)


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
    if ctype in {"roc_gte", "roc_lte"}:
        value = view.roc.get(int(cond["period"]))
        if value is None:
            return False
        if ctype == "roc_gte":
            return value >= float(cond["threshold"])
        return value <= float(cond["threshold"])
    if ctype in {"ema_gt_ema", "ema_lt_ema"}:
        fast = view.emas.get(int(cond["fast"]))
        slow = view.emas.get(int(cond["slow"]))
        if fast is None or slow is None:
            return False
        if ctype == "ema_gt_ema":
            return fast > slow
        return fast < slow
    return False


def updated_extreme(side: str, extreme: float, high: float, low: float) -> float:
    """Peak high (long) or trough low (short), ignoring non-finite prints."""
    if side == "long":
        if _finite(high) and high > extreme:
            return float(high)
        return float(extreme)
    if _finite(low) and low < extreme:
        return float(low)
    return float(extreme)


def trail_triggered(side: str, extreme: float, close: float, trail_pct: float) -> bool:
    """True when `close` has retraced `trail_pct` percent from `extreme`.

    `trail_pct` is percent points (5 means 5%). Longs trigger at or below
    peak × (1 − pct/100). Shorts trigger at or above trough × (1 + pct/100).
    A one-ulp dust band keeps a close that lands on the limit from missing it.
    """
    if not _finite(extreme) or not _finite(close) or extreme <= 0 or trail_pct <= 0:
        return False
    dist = float(trail_pct) / 100.0
    band = 1e-9 * max(1.0, abs(extreme))
    if side == "long":
        return close <= extreme * (1.0 - dist) + band
    if side == "short":
        return close >= extreme * (1.0 + dist) - band
    return False


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and value == value and abs(value) != float("inf")


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

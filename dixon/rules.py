"""Fixed-menu strategy rules (PRD v1.2).

Condition types:
  price_gt_ma, price_lt_ma     close vs SMA(window)
  ma_slope_gt_0, ma_slope_lt_0 SMA(window)_t vs SMA(window)_{t-1}
  ma_stack                     ordered windows, direction up (5>20>50) or down
  top50_spike                  symbol is inside that session's market Top-50
                               and the daily spike rule is true

Sides are long / flat / short, each an AND or OR of conditions.
Empty side never fires. Priority when several sides fire on the same bar:
  long and short together → flat (conflict);
  else long;
  else short;
  else flat-rule;
  else hold the current side.
"""

from __future__ import annotations

from typing import Any

MA_MENU: tuple[int, ...] = (5, 9, 20, 30, 50, 100, 250)
MA_MENU_SET = set(MA_MENU)

_WINDOWED = {"price_gt_ma", "price_lt_ma", "ma_slope_gt_0", "ma_slope_lt_0"}
_TYPES = _WINDOWED | {"ma_stack", "top50_spike"}
_SIDES = ("long", "flat", "short")


def _as_window(value: Any, label: str) -> int:
    try:
        window = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer MA window") from exc
    if window not in MA_MENU_SET:
        allowed = ", ".join(str(w) for w in MA_MENU)
        raise ValueError(f"{label} {window} is not in the fixed menu ({allowed})")
    return window


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
            conds.append(item)
        out[side] = {"logic": logic, "conditions": conds}
    return out


def windows_used(rules: dict[str, Any]) -> list[int]:
    found: set[int] = set()
    for side in _SIDES:
        for cond in rules.get(side, {}).get("conditions", []):
            if "window" in cond:
                found.add(int(cond["window"]))
            for w in cond.get("windows") or []:
                found.add(int(w))
    return sorted(found)


def condition_true(
    cond: dict[str, Any],
    close: float,
    mas: dict[int, float | None],
    mas_prev: dict[int, float | None],
    spike: bool,
) -> bool:
    ctype = cond["type"]
    if ctype == "top50_spike":
        return bool(spike)
    if ctype == "price_gt_ma":
        ma = mas.get(cond["window"])
        return ma is not None and close > ma
    if ctype == "price_lt_ma":
        ma = mas.get(cond["window"])
        return ma is not None and close < ma
    if ctype == "ma_slope_gt_0":
        cur = mas.get(cond["window"])
        prev = mas_prev.get(cond["window"])
        return cur is not None and prev is not None and cur > prev
    if ctype == "ma_slope_lt_0":
        cur = mas.get(cond["window"])
        prev = mas_prev.get(cond["window"])
        return cur is not None and prev is not None and cur < prev
    if ctype == "ma_stack":
        vals = [mas.get(w) for w in cond["windows"]]
        if any(v is None for v in vals):
            return False
        if cond.get("direction", "up") == "down":
            return all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))  # type: ignore[operator]
        return all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))  # type: ignore[operator]
    return False


def _group_true(
    group: dict[str, Any],
    close: float,
    mas: dict[int, float | None],
    mas_prev: dict[int, float | None],
    spike: bool,
) -> bool:
    conds = group.get("conditions") or []
    if not conds:
        return False
    flags = [condition_true(c, close, mas, mas_prev, spike) for c in conds]
    if group.get("logic") == "OR":
        return any(flags)
    return all(flags)


def target_side(
    rules: dict[str, Any],
    close: float,
    mas: dict[int, float | None],
    mas_prev: dict[int, float | None],
    spike: bool,
    current: str,
) -> str:
    """Desired side after this bar's close. `current` is held when nothing fires."""
    long_on = _group_true(rules["long"], close, mas, mas_prev, spike)
    short_on = _group_true(rules["short"], close, mas, mas_prev, spike)
    flat_on = _group_true(rules["flat"], close, mas, mas_prev, spike)
    if long_on and short_on:
        return "flat"
    if long_on:
        return "long"
    if short_on:
        return "short"
    if flat_on:
        return "flat"
    return current

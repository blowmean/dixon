# Project Dixon — Indicator Formulas (v1)

**Purpose:** Exact formulas QA can test against.  
**Posture:** Best-effort vs Yahoo Finance math. **Not** TradingView-certified.  
**Input series:** Split/dividend-adjusted daily `close` from `daily_bars` (Yahoo `auto_adjust=True`).

All indicators are computed on the **daily** adjusted close series and stored in `indicator_cache` keyed by `(market, symbol, trade_date, name)`.

Warm-up: values are omitted (not written) until `min_periods` is satisfied.

---

## Notation

- \(C_t\) = adjusted close on session \(t\) (chronological trading sessions present in DB)
- \(n\) = lookback window / period
- Sessions with no Yahoo bar are **skipped** (no forward-fill)

---

## Simple Moving Average — `MA_{n}`

\[
\mathrm{MA}_n(t) = \frac{1}{n}\sum_{i=0}^{n-1} C_{t-i}
\]

- Implementation: `pandas.Series.rolling(window=n, min_periods=n).mean()`
- Default names: `MA_5`, `MA_9`, `MA_20`, `MA_30`, `MA_50`, `MA_100`, `MA_200`, `MA_250`
- Strategy fixed menu (conditions only): **5, 9, 20, 30, 50, 100, 200, 250**. `MA_200` is the Double Seven trend gate (`price_gt_ma` window 200) and a chart overlay.
- First valid point: the \(n\)-th available close
- **Timeframe:** the same formula runs on daily, weekly, or monthly closes. Weekly/monthly rows are stored as `W|MA_n` / `M|MA_n` so they do not collide with the daily cache. The API still returns the logical name `MA_n` plus `timeframe`. First request computes and writes `indicator_cache` + `indicator_meta`; a later request reuses the rows while the bar fingerprint (count, last date, close sum, last close) is unchanged.

---

## Exponential Moving Average — `EMA_{n}`

Recursive form with \(\alpha = 2 / (n + 1)\):

\[
\mathrm{EMA}_n(t) = \alpha\, C_t + (1 - \alpha)\, \mathrm{EMA}_n(t-1)
\]

- Seed: `pandas` `ewm(span=n, adjust=False, min_periods=n)`  
  (first EMA equals the SMA seed behaviour of pandas `adjust=False` after `min_periods`)
- Default names: `EMA_12`, `EMA_26`

---

## Relative Strength Index — `RSI_14` (Wilder)

1. \( \Delta_t = C_t - C_{t-1} \)
2. \( \mathrm{gain}_t = \max(\Delta_t, 0) \), \( \mathrm{loss}_t = \max(-\Delta_t, 0) \)
3. Wilder smooth (\(\alpha = 1/14\)):

\[
\overline{G}_t = \alpha\,\mathrm{gain}_t + (1-\alpha)\,\overline{G}_{t-1}
\]
\[
\overline{L}_t = \alpha\,\mathrm{loss}_t + (1-\alpha)\,\overline{L}_{t-1}
\]

4. \( \mathrm{RS}_t = \overline{G}_t / \overline{L}_t \)

\[
\mathrm{RSI}_t = 100 - \frac{100}{1 + \mathrm{RS}_t}
\]

Special cases:

- \(\overline{L}_t = 0\) and \(\overline{G}_t > 0\) → RSI = 100
- \(\overline{L}_t = 0\) and \(\overline{G}_t = 0\) → RSI = 50

Implementation: `ewm(alpha=1/period, adjust=False, min_periods=period)`.

---

## MACD — `MACD_12_26_9`, `MACD_SIGNAL_12_26_9`, `MACD_HIST_12_26_9`

\[
\mathrm{MACD}_t = \mathrm{EMA}_{12}(C)_t - \mathrm{EMA}_{26}(C)_t
\]
\[
\mathrm{Signal}_t = \mathrm{EMA}_{9}(\mathrm{MACD})_t
\]
\[
\mathrm{Hist}_t = \mathrm{MACD}_t - \mathrm{Signal}_t
\]

Cached names:

| Name | Series |
| --- | --- |
| `MACD_12_26_9` | MACD line |
| `MACD_SIGNAL_12_26_9` | Signal line |
| `MACD_HIST_12_26_9` | Histogram |

---

## Bollinger Bands — `BB_*_20_2`

Population std (`ddof=0`) over the same window as the middle band:

\[
\mathrm{Mid}_t = \mathrm{MA}_{20}(t)
\]
\[
\sigma_t = \sqrt{\frac{1}{20}\sum_{i=0}^{19}(C_{t-i} - \mathrm{Mid}_t)^2}
\]
\[
\mathrm{Upper}_t = \mathrm{Mid}_t + 2\sigma_t,\quad
\mathrm{Lower}_t = \mathrm{Mid}_t - 2\sigma_t
\]

Cached names: `BB_MID_20_2`, `BB_UPPER_20_2`, `BB_LOWER_20_2`.

---

## Weekly / monthly bar rollup (for QA of W/M consistency)

Not indicators, but acceptance-critical:

- **Weekly:** group daily bars by ISO year-week;  
  open = first open, high = max high, low = min low, close = last close, volume = sum;  
  `period_end` = last `trade_date` in the group.
- **Monthly:** group by calendar year-month; same aggregation.
- Exchange calendars (NYSE / HKEX) define which sessions *can* exist; we only roll dates that Yahoo actually delivered.

See `dixon/calendars.py` and `dixon/rollup.py`.

---

## Strategy predicates (single symbol)

These are evaluated on the strategy timeframe's stored bars (daily, weekly, or monthly). They are not a second cross-sectional book. RSI is the cached Wilder series above (`RSI_{period}`). Volume SMA and Donchian levels are computed from that symbol's OHLC when a backtest needs them; they are not written into `indicator_cache`.

### Relative volume — `rel_volume`

\[
V_t \ge k \times \mathrm{SMA}_N(V)_t
\]

\[
\mathrm{SMA}_N(V)_t = \frac{1}{N}\sum_{i=0}^{N-1} V_{t-i}
\]

- Defaults: \(k = 1.5\), \(N = 20\). Both are stored on the condition.
- The signal bar's volume is inside the average.
- Warm-up (fewer than \(N\) volumes) or a non-positive average → condition is false.

### RSI threshold — `rsi_lte` / `rsi_gte`

Same Wilder RSI as `RSI_14`, with caller-supplied `period` (default 2) and `threshold` (default 5 for `rsi_lte`, 70 for `rsi_gte`).

- `rsi_lte`: \(\mathrm{RSI}(\mathrm{period})_t \le \mathrm{threshold}\)
- `rsi_gte`: \(\mathrm{RSI}(\mathrm{period})_t \ge \mathrm{threshold}\)
- Missing warm-up value → false.

A Connors-style entry is `rsi_lte` with period 2 and threshold 5. The exit can be the existing `price_gt_ma` (close > SMA(5)) or `rsi_gte`.

### Donchian channel — `donchian_high` / `donchian_low`

Prior channel, **excluding** the signal bar (a close cannot exceed that bar's own high):

\[
\mathrm{High}^{(N)}_t = \max(H_{t-N},\ldots,H_{t-1})
\]
\[
\mathrm{Low}^{(M)}_t = \min(L_{t-M},\ldots,L_{t-1})
\]

- `donchian_high`: \(C_t > \mathrm{High}^{(N)}_t\). Default \(N = 20\).
- `donchian_low`: \(C_t < \mathrm{Low}^{(M)}_t\). Default \(M = 10\).
- Warm-up → false. No ATR stop is applied.

### Close extreme — `close_lowest` / `close_highest`

The last N **closes**, including the signal bar. This is not a Donchian channel: Donchian compares the close with prior **highs** or **lows** and leaves today out.

\[
\mathrm{CloseMin}^{(N)}_t = \min(C_{t-N+1},\ldots,C_t)
\]
\[
\mathrm{CloseMax}^{(N)}_t = \max(C_{t-N+1},\ldots,C_t)
\]

- `close_lowest`: \(C_t = \mathrm{CloseMin}^{(N)}_t\). Default \(N = 7\).
- `close_highest`: \(C_t = \mathrm{CloseMax}^{(N)}_t\). Default \(N = 7\).
- \(N\) is an integer from 1 to 500 (same span as Donchian), so lookbacks 4–7 can be swept. It is not an SMA-menu window.
- Warm-up (fewer than \(N\) closes) → false.
- A tie counts: if two closes share the extreme, both bars are true.
- When every close in the window is equal, both predicates are true. Long still beats flat, so a flat tape does not exit.

Connors Double Seven is `close_lowest` with \(N = 7\) ANDed with `price_gt_ma` window **200** on `long`, and `close_highest` with \(N = 7\) on `flat`. No `trail_pct`. The average only gates entries. An open long is held if price later closes at or below SMA(200), until an N-day high close. The fill is the next bar's open.

A close can be the N-day closing low while it stays above the prior N-bar low (a wick), and the N-day closing high while it stays below the prior N-bar high. Those bars are true for the close extreme and false for Donchian.

### Rate of change — `roc_gte` / `roc_lte`

Percent points versus the close `period` bars ago (the signal bar is included; the reference bar is not):

\[
\mathrm{ROC}(\mathrm{period})_t = \left(\frac{C_t}{C_{t-\mathrm{period}}} - 1\right) \times 100
\]

- `roc_gte`: \(\mathrm{ROC}(\mathrm{period})_t \ge \mathrm{threshold}\). Defaults: period 7, threshold **+1** (+1%).
- `roc_lte`: \(\mathrm{ROC}(\mathrm{period})_t \le \mathrm{threshold}\). Defaults: period 7, threshold **−1** (−1%).
- Threshold units are percent points. `1` means +1%, not 100% and not the fraction `0.01`.
- Period is an integer from 1 to 500 (same lookback span as relative volume and Donchian, not the fixed MA menu). Threshold is −100 to 10000.
- Warm-up, or a non-positive reference close → false.
- These are ordinary side predicates. `long` AND `roc_gte` with `rel_volume`, `rsi_lte` / `rsi_gte`, Donchian, or an MA rule requires every condition.

A K2-style entry is `roc_gte` period 7 threshold 1 on `long`, and `roc_lte` period 7 threshold −1 on `short`. The band between −1% and +1% does not fire, so an open position is held until a trail, a flat rule, or the opposite side.

### EMA stack — `ema_gt_ema` / `ema_lt_ema`

Same recursive EMA as `EMA_{n}` above (`ewm(span, adjust=False, min_periods=span)`), computed on the strategy timeframe's closes. The signal bar is included. Warm-up values (fewer than `span` closes) are missing and the condition is false.

- `ema_gt_ema`: \(\mathrm{EMA}(\mathrm{fast})_t > \mathrm{EMA}(\mathrm{slow})_t\). Defaults: fast **9**, slow **21**.
- `ema_lt_ema`: \(\mathrm{EMA}(\mathrm{fast})_t < \mathrm{EMA}(\mathrm{slow})_t\). Same defaults.
- Spans are integers from 1 to 500. They are not SMA-menu windows, so 21 is valid. `fast` and `slow` must differ. The names are the two spans, not a requirement that `fast` be the shorter one: `ema_gt_ema` with fast 21 and slow 9 is EMA(21) > EMA(9).
- Equal values are false for both types, so a flat market does not flip a position.
- The predicate is the stack, true on every bar the inequality holds. It is not limited to the crossing bar. Under the hold-when-silent rule, a one-bar cross would stay in after the stack had already flipped; the inequality is what "long when the fast EMA is above the slow EMA" means.
- Volume is a separate condition. The public pattern ANDs `rel_volume` with k **1.5** and window **20** on long (`ema_gt_ema`) and on short (`ema_lt_ema`). A bar where the stack has flipped but volume is below 1.5× its average does not fire, so the open side is held. `k` **1** is volume ≥ SMA(volume, 20), still the existing `≥` test.

A three-average stack is two pairwise conditions on the same side (`logic: AND`), for example EMA(9) > EMA(21) and EMA(21) > EMA(50).

### RSI slope streak — `rsi_falling_n` / `rsi_rising_n`

Same Wilder RSI as `RSI_{period}` (`rsi_wilder`). The signal bar's close is included. High, low, open, and volume are not. `n` counts **strict steps**, so the run ending at \(t\) reads \(n+1\) RSI values:

\[
\mathrm{Falling}(\mathrm{period}, n)_t
  = \bigwedge_{k=0}^{n-1}
    \bigl(\mathrm{RSI}(\mathrm{period})_{t-k} < \mathrm{RSI}(\mathrm{period})_{t-k-1}\bigr)
\]
\[
\mathrm{Rising}(\mathrm{period}, n)_t
  = \bigwedge_{k=0}^{n-1}
    \bigl(\mathrm{RSI}(\mathrm{period})_{t-k} > \mathrm{RSI}(\mathrm{period})_{t-k-1}\bigr)
\]

- `rsi_falling_n`: the falling run. Defaults: period **14**, \(n\) **3**.
- `rsi_rising_n`: the rising run. Same defaults.
- Period is an integer from 1 to 250, the same bounds as `rsi_lte` / `rsi_gte`. \(n\) is an integer from 1 to 500. \(n = 1\) is the one-bar slope, \(\mathrm{RSI}_t < \mathrm{RSI}_{t-1}\) or the reverse. \(n = 3\) is three consecutive steps.
- An equal RSI is not a step, so a flat print breaks the run. Warm-up (any of the \(n+1\) RSI values missing) → false.
- These predicates are true only on the bars that complete the run. Under the hold-when-silent rule, the open side stays on after the streak ends until the opposite streak, a flat rule, or a trail. The same period cannot rise and fall on one bar. Two periods can, and that conflict goes flat.
- The public pattern is `rsi_rising_n` period 14 \(n\) 3 on `long`, and `rsi_falling_n` with the same knobs on `short`. No `trail_pct`. US/HK cost defaults are unchanged.

This is **not** a z-score of RSI, and it is not `rsi_lte` / `rsi_gte`.

A z-score of the RSI level (backlog S1-K16, not implemented) would be

\[
z_t(W) = \frac{\mathrm{RSI}_t - \mu_W(\mathrm{RSI})_t}{\sigma_W(\mathrm{RSI})_t}
\]

with \(\mu_W\) and \(\sigma_W\) the mean and standard deviation of RSI over a window \(W\). The sign of \(z\) is the sign of \(\mathrm{RSI}_t - \mu_W\): the level versus its own recent history. \(N\) on the slope streak is not that window.

A high RSI can still be falling. Readings that sit at 50 and then print 80, 78, 76, 74 are a 3-step fall, so `rsi_falling_n` with \(n = 3\) is true on the last bar, while those readings are above a trailing mean near 55, so \(z > 0\). A later 40, 42, 44, 46 is a 3-step rise, so `rsi_rising_n` is true and `rsi_falling_n` is false, while 46 is still below that mean, so \(z < 0\). `rsi_lte` / `rsi_gte` only compare the level with a constant threshold. They do not look at the slope either.

### Williams %R — `willr_lte` / `willr_gte`

Larry Williams' oscillator on the last \(N\) highs and lows, **including** the signal bar. Not a cached chart series: the backtest builds it from that symbol's OHLC, the same way Donchian and ROC are built.

\[
HH_t(N) = \max(H_{t-N+1},\ldots,H_t)
\]
\[
LL_t(N) = \min(L_{t-N+1},\ldots,L_t)
\]
\[
\%R(N)_t = \frac{HH_t(N) - C_t}{HH_t(N) - LL_t(N)} \times (-100)
\]

- A close at the window high is 0. A close at the window low is −100. Readings live on that negative scale.
- `willr_lte`: \(\%R(N)_t \le \mathrm{threshold}\). The compare is inclusive. Defaults: \(N\) **7**, threshold **−95**. A print of exactly −95 is true.
- `willr_gte`: \(\%R(N)_t \ge \mathrm{threshold}\). The compare is inclusive. Defaults: \(N\) **7**, threshold **−5**. A print of exactly −5 is true.
- \(N\) is an integer from 1 to 500. The threshold is a number from −100 to 0, the oscillator itself, not a percent return. `−95` means the −95 line.
- The signal bar is inside the window. Donchian drops today and compares the close with a prior high or low, so the two disagree when today's wick sets the extreme.
- Warm-up (fewer than \(N\) bars), a missing high or low in the window, or a zero range \(HH_t = LL_t\) → false. The ratio is undefined when the range is flat.
- On a positive range this is stochastic \(\%K - 100\), where \(\%K = (C - LL) / (HH - LL) \times 100\). Dixon compares the Williams reading. It does not store \(\%K\).

The public pattern is the StatOasis scout cell: `willr_lte` \(N=7\) threshold −95 on `long`, and `willr_gte` \(N=7\) threshold −5 on `flat`. No `trail_pct`. While \(\%R\) is strictly between −95 and −5 neither side fires, so an open position is held. A long that is still at or below −95 is not flattened by the −5 rule, because a true long beats flat. Omitted knobs store these public values. A second preset, Williams %R(14) classic, writes \(N=14\), −80, and −20 on the conditions; those are not the omitted defaults. US/HK cost defaults are unchanged. The overnight `s1-board-v1` grid does not include either pattern.

### Trailing stop — `trail_pct`

Optional number on the rules object, not a condition. Percent points: `5` means 5%. Omit or null to disable.

While a position is open, track the extreme since the fill (the entry price, then each bar's high or low):

- Long: peak \(P_t = \max(P_{t-1}, H_t)\). Exit signal when \(C_t \le P_t \times (1 - \mathrm{trail\_pct}/100)\).
- Short: trough \(T_t = \min(T_{t-1}, L_t)\). Exit signal when \(C_t \ge T_t \times (1 + \mathrm{trail\_pct}/100)\).

The breach forces flat even when the held side's entry predicate is still true. An opposite-side signal on that same close reverses instead. The fill is the next stored bar's open (`exit_reason` `trail`); the exit price is that open, not the stop level. A breach on the last in-range bar does not flatten. The ROC(7) public pattern uses `trail_pct` 5. US/HK cost defaults are unchanged.

---

## QA tips

1. Compare golden symbols (`0700.HK`, `0005.HK`, `AAPL`, `MSFT`, `SPY`) adjusted daily close vs Yahoo within 1 tick.
2. Recompute `MA_20` / `RSI_14` independently from exported closes; match `indicator_cache` within float tolerance (~1e-8 relative or 1e-4 absolute).
3. Yahoo chart overlays may differ slightly (seed / rounding); Dixon is authoritative for Dixon QA.

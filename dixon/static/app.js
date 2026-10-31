/* Project Dixon charts UI — lightweight-charts + /bars + /indicators */
(function () {
  "use strict";

  const GOLDEN = ["0700.HK", "0005.HK", "AAPL", "MSFT", "SPY"];
  const MAX_RESULTS = 50;

  const IND_COLORS = {
    MA_5: "#f8fafc",
    MA_9: "#fb7185",
    MA_20: "#f59e0b",
    MA_30: "#facc15",
    MA_50: "#3b82f6",
    MA_100: "#34d399",
    MA_200: "#a78bfa",
    MA_250: "#a855f7",
    EMA_12: "#22d3ee",
    EMA_26: "#e879f9",
    BB_MID_20_2: "#94a3b8",
    BB_UPPER_20_2: "#64748b",
    BB_LOWER_20_2: "#64748b",
    RSI_14: "#eab308",
    MACD_12_26_9: "#3b82f6",
    MACD_SIGNAL_12_26_9: "#f59e0b",
    MACD_HIST_12_26_9: "#26a69a",
  };

  const state = {
    priceChart: null,
    rsiChart: null,
    macdChart: null,
    candleSeries: null,
    volumeSeries: null,
    priceLines: {},
    rsiSeries: null,
    macdLine: null,
    macdSignal: null,
    macdHist: null,
    barDates: new Set(),
    // symbol picker
    allSymbols: [], // [{symbol, market}]
    filtered: [],
    activeIndex: -1,
    listOpen: false,
    equityChart: null,
    strategies: [],
    editingId: null,
    chartBox: "",
  };

  const MA_MENU = [5, 9, 20, 30, 50, 100, 250];
  const COND_TYPES = [
    ["price_gt_ma", "Price > MA"],
    ["price_lt_ma", "Price < MA"],
    ["ma_slope_gt_0", "MA slope > 0"],
    ["ma_slope_lt_0", "MA slope < 0"],
    ["ma_stack", "MA stack"],
    ["top50_spike", "In Top-50 spike"],
    ["rel_volume", "Rel volume ≥ k×SMA"],
    ["rsi_lte", "RSI ≤ threshold"],
    ["rsi_gte", "RSI ≥ threshold"],
    ["donchian_high", "Close > prior N high"],
    ["donchian_low", "Close < prior M low"],
    ["roc_gte", "ROC ≥ threshold"],
    ["roc_lte", "ROC ≤ threshold"],
  ];
  const PREDICATE_DEFAULTS = {
    rel_volume: { k: 1.5, window: 20 },
    rsi_lte: { period: 2, threshold: 5 },
    rsi_gte: { period: 2, threshold: 70 },
    donchian_high: { window: 20 },
    donchian_low: { window: 10 },
    roc_gte: { period: 7, threshold: 1 },
    roc_lte: { period: 7, threshold: -1 },
  };

  function $(id) {
    return document.getElementById(id);
  }

  function setStatus(msg, kind) {
    const el = $("status");
    el.textContent = msg || "";
    el.className = kind || "";
  }

  function apiHeaders() {
    const h = { Accept: "application/json" };
    const key = $("apiKey").value.trim() || localStorage.getItem("dixon_api_key") || "";
    if (key) h["X-API-Key"] = key;
    return h;
  }

  async function apiGet(path) {
    const res = await fetch(path, { headers: apiHeaders() });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${path}: ${text.slice(0, 200)}`);
    }
    return res.json();
  }

  function toUnix(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    return Math.floor(Date.UTC(y, m - 1, d) / 1000);
  }

  function businessDay(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    return { year: y, month: m, day: d };
  }

  function alignSeries(series, barDates) {
    return (series || [])
      .filter((p) => barDates.has(p.date) && p.value != null && !Number.isNaN(p.value))
      .map((p) => ({ time: businessDay(p.date), value: p.value }));
  }

  function inferMarket(sym) {
    const s = String(sym || "").toUpperCase();
    if (s.endsWith(".HK") || s.endsWith(".HKG")) return "HK";
    return "US";
  }

  function marketBadgeClass(market) {
    const m = String(market || "").toUpperCase();
    if (m === "HK" || m === "HKG") return "hk";
    if (m === "US" || m === "USA") return "us";
    return "";
  }

  function marketLabel(market) {
    const m = String(market || "").toUpperCase();
    if (m === "HK" || m === "HKG") return "HK";
    if (m === "US" || m === "USA") return "US";
    return m || "?";
  }

  function chartOpts(heightHint) {
    return {
      layout: {
        background: { type: "solid", color: "#0f1419" },
        textColor: "#8b9bb0",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a3544" },
      timeScale: { borderColor: "#2a3544", timeVisible: false },
      height: heightHint,
    };
  }

  // Each pane is its own chart. A shared logical range lines up dates only when
  // every pane has the same bar times, and only when the plot areas have the
  // same width. Indicator warm-up used to drop leading bars (so index 0 was a
  // different day), and a wider right scale (MACD's extra decimals) squeezed
  // that pane's plot. Whitespace keeps the timeline; minimumWidth shares the edge.
  let syncingTime = false;
  let syncingCrosshair = false;

  function paneCharts() {
    return [state.priceChart, state.rsiChart, state.macdChart].filter(Boolean);
  }

  function chartIsShown(id) {
    const el = $(id);
    return !!el && el.style.display !== "none";
  }

  function visiblePaneCharts() {
    const charts = [];
    if (state.priceChart && chartIsShown("price-chart")) charts.push(state.priceChart);
    if (state.rsiChart && chartIsShown("rsi-chart")) charts.push(state.rsiChart);
    if (state.macdChart && chartIsShown("macd-chart")) charts.push(state.macdChart);
    return charts;
  }

  function alignRightPriceScales() {
    const charts = visiblePaneCharts();
    if (charts.length < 2) return;
    let max = 0;
    const widths = charts.map((chart) => {
      const width = chart.priceScale("right").width();
      if (width > max) max = width;
      return width;
    });
    if (max < 1 || widths.every((width) => max - width <= 0.5)) return;
    charts.forEach((chart) => {
      chart.priceScale("right").applyOptions({ minimumWidth: max });
    });
  }

  function chartId(chart) {
    if (chart === state.priceChart) return "price-chart";
    if (chart === state.rsiChart) return "rsi-chart";
    if (chart === state.macdChart) return "macd-chart";
    return "";
  }

  function syncTimeScale(src, range) {
    if (syncingTime || !src || !range) return;
    // Hidden panes have no plot width. Pushing a range onto one (or copying a
    // range out of one after setData) shifts the logical index base, and the
    // next time that pane is shown every chart scrolls past the last bar.
    if (!chartIsShown(chartId(src))) return;
    const charts = visiblePaneCharts();
    if (charts.indexOf(src) < 0) return;
    syncingTime = true;
    try {
      // Forward the range from the callback. getVisibleLogicalRange() still
      // returns the previous range for a frame, and writing that back scrolls
      // the panes into empty space.
      alignRightPriceScales();
      charts.forEach((chart) => {
        if (chart === src) return;
        try {
          chart.timeScale().setVisibleLogicalRange(range);
        } catch (_) {}
      });
    } finally {
      syncingTime = false;
    }
  }

  function fitPanes() {
    const price = state.priceChart;
    if (!price) return;
    // fitContent does not notify when the price pane is already fitted. A pane
    // that just received bars would otherwise keep its own default range.
    let notified = false;
    const mark = () => {
      notified = true;
    };
    price.timeScale().subscribeVisibleLogicalRangeChange(mark);
    price.timeScale().fitContent();
    price.timeScale().unsubscribeVisibleLogicalRangeChange(mark);
    if (notified) return;
    const range = price.timeScale().getVisibleLogicalRange();
    if (!range) return;
    syncingTime = true;
    try {
      alignRightPriceScales();
      visiblePaneCharts().forEach((chart) => {
        if (chart === price) return;
        try {
          chart.timeScale().setVisibleLogicalRange(range);
        } catch (_) {}
      });
    } finally {
      syncingTime = false;
    }
  }

  function seriesForChart(chart) {
    if (chart === state.priceChart) return state.candleSeries;
    if (chart === state.rsiChart) return state.rsiSeries;
    return state.macdLine;
  }

  function priceOfPoint(point) {
    if (!point) return null;
    if (point.value != null && !Number.isNaN(point.value)) return point.value;
    if (point.close != null && !Number.isNaN(point.close)) return point.close;
    return null;
  }

  function priceAtLogical(series, logical) {
    if (!series || logical == null || Number.isNaN(logical)) return null;
    const index = Math.round(logical);
    let point = series.dataByIndex(index);
    if (priceOfPoint(point) == null) {
      point = series.dataByIndex(index, LightweightCharts.MismatchDirection.NearestLeft);
    }
    return priceOfPoint(point);
  }

  function syncCrosshair(src, param) {
    if (syncingCrosshair) return;
    const charts = visiblePaneCharts();
    syncingCrosshair = true;
    try {
      charts.forEach((chart) => {
        if (chart === src) return;
        const series = seriesForChart(chart);
        const time = param && param.time;
        if (!series || time == null) {
          chart.clearCrosshairPosition();
          return;
        }
        let price = priceAtLogical(series, param.logical);
        if (price == null && chart.paneSize) {
          const size = chart.paneSize();
          price = size ? series.coordinateToPrice(size.height / 2) : null;
        }
        if (price == null || Number.isNaN(price)) {
          chart.clearCrosshairPosition();
          return;
        }
        chart.setCrosshairPosition(price, time, series);
      });
    } catch (_) {
      /* a pane that has no bars yet has nothing to anchor */
    } finally {
      syncingCrosshair = false;
    }
  }

  function panePoints(series, orderedDates) {
    const values = new Map();
    (series || []).forEach((p) => {
      if (!p || p.date == null || p.value == null || Number.isNaN(Number(p.value))) return;
      values.set(p.date, Number(p.value));
    });
    return orderedDates.map((date) => {
      const time = businessDay(date);
      const value = values.get(date);
      return value == null ? { time } : { time, value };
    });
  }

  function ensureCharts() {
    if (state.priceChart) return;

    const priceEl = $("price-chart");
    const rsiEl = $("rsi-chart");
    const macdEl = $("macd-chart");

    state.priceChart = LightweightCharts.createChart(priceEl, chartOpts(priceEl.clientHeight || 360));
    state.candleSeries = state.priceChart.addCandlestickSeries({
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    });
    state.priceChart.priceScale("right").applyOptions({
      scaleMargins: { top: 0.08, bottom: 0.25 },
    });
    state.volumeSeries = state.priceChart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    state.priceChart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
    });

    state.rsiChart = LightweightCharts.createChart(rsiEl, chartOpts(rsiEl.clientHeight || 140));
    state.rsiSeries = state.rsiChart.addLineSeries({
      color: IND_COLORS.RSI_14,
      lineWidth: 2,
      priceLineVisible: false,
    });
    state.rsiSeries.createPriceLine({ price: 70, color: "#334155", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
    state.rsiSeries.createPriceLine({ price: 30, color: "#334155", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });

    state.macdChart = LightweightCharts.createChart(macdEl, chartOpts(macdEl.clientHeight || 140));
    state.macdHist = state.macdChart.addHistogramSeries({
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    state.macdLine = state.macdChart.addLineSeries({
      color: IND_COLORS.MACD_12_26_9,
      lineWidth: 2,
      priceLineVisible: false,
    });
    state.macdSignal = state.macdChart.addLineSeries({
      color: IND_COLORS.MACD_SIGNAL_12_26_9,
      lineWidth: 2,
      priceLineVisible: false,
    });

    paneCharts().forEach((src) => {
      src.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        syncTimeScale(src, range);
      });
      src.subscribeCrosshairMove((param) => {
        syncCrosshair(src, param);
      });
    });

    const chartColumn = document.querySelector("main");
    window.addEventListener("resize", resizeCharts);
    if (typeof ResizeObserver !== "undefined" && chartColumn) {
      new ResizeObserver(() => resizeCharts()).observe(chartColumn);
    }
    resizeCharts();
  }

  function resizeCharts() {
    const main = document.querySelector("main");
    if (!main || !state.priceChart) return;
    const showRsi = $("rsi-chart").style.display !== "none";
    const showMacd = $("macd-chart").style.display !== "none";
    const w = main.clientWidth;
    const h = main.clientHeight;
    const box = w + "x" + h + ":" + showRsi + ":" + showMacd;
    if (box === state.chartBox || w < 2 || h < 2) return;
    state.chartBox = box;
    let panes = 1 + (showRsi ? 1 : 0) + (showMacd ? 1 : 0);
    const priceH = Math.floor(h * (panes === 1 ? 1 : panes === 2 ? 0.7 : 0.55));
    const otherH = Math.floor((h - priceH) / Math.max(1, panes - 1));
    state.priceChart.applyOptions({ width: w, height: priceH });
    if (showRsi) state.rsiChart.applyOptions({ width: w, height: otherH });
    if (showMacd) state.macdChart.applyOptions({ width: w, height: otherH });
    if (state.equityChart) {
      const eq = $("equity-chart");
      if (eq && eq.clientWidth) state.equityChart.applyOptions({ width: eq.clientWidth });
    }
  }

  function clearPriceOverlays() {
    Object.values(state.priceLines).forEach((s) => {
      try {
        state.priceChart.removeSeries(s);
      } catch (_) {}
    });
    state.priceLines = {};
  }

  function addLine(name, data, color, options) {
    if (!data.length) return;
    const series = state.priceChart.addLineSeries({
      color: color || "#94a3b8",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      ...options,
    });
    series.setData(data);
    state.priceLines[name] = series;
  }

  function selectedIndicators() {
    const names = [];
    document.querySelectorAll('.overlays input[data-ind]').forEach((cb) => {
      if (!cb.checked) return;
      const key = cb.getAttribute("data-ind");
      if (key === "BB") {
        names.push("BB_MID_20_2", "BB_UPPER_20_2", "BB_LOWER_20_2");
      } else {
        names.push(key);
      }
    });
    return names;
  }

  function paneEnabled(name) {
    const el = document.querySelector(`.overlays input[data-pane="${name}"]`);
    return el ? el.checked : true;
  }

  async function fetchIndicators(symbol, names, from, to, timeframe) {
    const q = new URLSearchParams({
      symbol,
      names: names.join(","),
      timeframe: timeframe || "daily",
    });
    if (from) q.set("from", from);
    if (to) q.set("to", to);
    return apiGet(`/indicators?${q}`);
  }

  async function apiSend(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { ...apiHeaders(), "Content-Type": "application/json" },
      body: body == null ? undefined : JSON.stringify(body),
    });
    if (res.status === 204) return null;
    const text = await res.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (_) {
      payload = null;
    }
    if (!res.ok) {
      const detail = payload && payload.detail ? payload.detail : text.slice(0, 200);
      throw new Error(`${res.status} ${path}: ${detail}`);
    }
    return payload;
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  /* ---------- Searchable symbol picker ---------- */

  function filterSymbols(query) {
    const q = String(query || "").trim().toUpperCase();
    let list = state.allSymbols;
    if (q) {
      const scored = [];
      for (let i = 0; i < list.length; i++) {
        const item = list[i];
        const sym = item.symbol.toUpperCase();
        const mkt = String(item.market || "").toUpperCase();
        let score = -1;
        if (sym === q) score = 0;
        else if (sym.startsWith(q)) score = 1;
        else if (sym.includes(q)) score = 2;
        else if (mkt && mkt.startsWith(q)) score = 3;
        else if (mkt && mkt.includes(q)) score = 4;
        // Also match bare HK codes without suffix: "0700" → "0700.HK"
        else if (sym.replace(/\.HK$/, "").startsWith(q)) score = 1;
        if (score >= 0) scored.push({ item, score, sym });
      }
      scored.sort((a, b) => a.score - b.score || a.sym.localeCompare(b.sym));
      list = scored.map((s) => s.item);
    }
    return list.slice(0, MAX_RESULTS);
  }

  function renderSymbolList() {
    const ul = $("symbolList");
    ul.innerHTML = "";
    if (!state.filtered.length) {
      const li = document.createElement("li");
      li.className = "symbol-empty";
      li.textContent = "No matching symbols";
      ul.appendChild(li);
      state.activeIndex = -1;
      return;
    }
    state.filtered.forEach((item, i) => {
      const li = document.createElement("li");
      li.className = "symbol-option" + (i === state.activeIndex ? " active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("data-index", String(i));
      li.setAttribute("aria-selected", i === state.activeIndex ? "true" : "false");

      const sym = document.createElement("span");
      sym.className = "sym";
      sym.textContent = item.symbol;

      const badge = document.createElement("span");
      badge.className = "market-badge " + marketBadgeClass(item.market);
      badge.textContent = marketLabel(item.market);

      li.appendChild(sym);
      li.appendChild(badge);
      li.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep focus on input
        pickSymbol(item);
      });
      ul.appendChild(li);
    });
  }

  function openList() {
    const ul = $("symbolList");
    const input = $("symbolInput");
    ul.hidden = false;
    input.setAttribute("aria-expanded", "true");
    state.listOpen = true;
  }

  function closeList() {
    const ul = $("symbolList");
    const input = $("symbolInput");
    ul.hidden = true;
    input.setAttribute("aria-expanded", "false");
    state.listOpen = false;
    state.activeIndex = -1;
  }

  function setActiveIndex(idx) {
    if (!state.filtered.length) {
      state.activeIndex = -1;
      return;
    }
    const n = state.filtered.length;
    state.activeIndex = ((idx % n) + n) % n;
    renderSymbolList();
    const active = $("symbolList").querySelector(".symbol-option.active");
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  function pickSymbol(item, opts) {
    if (!item) return;
    const silent = opts && opts.silent;
    $("symbol").value = item.symbol;
    $("symbolInput").value = item.symbol;
    closeList();
    if (!silent) loadChart();
  }

  function refreshFilterFromInput() {
    state.filtered = filterSymbols($("symbolInput").value);
    state.activeIndex = state.filtered.length ? 0 : -1;
    renderSymbolList();
    openList();
  }

  function wireSymbolPicker() {
    const input = $("symbolInput");
    const combo = $("symbolCombo");

    input.addEventListener("focus", () => {
      state.filtered = filterSymbols(input.value);
      state.activeIndex = state.filtered.length ? 0 : -1;
      renderSymbolList();
      openList();
      // Select all so typing replaces quickly
      input.select();
    });

    input.addEventListener("input", () => {
      refreshFilterFromInput();
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!state.listOpen) {
          refreshFilterFromInput();
        } else {
          setActiveIndex(state.activeIndex + 1);
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (!state.listOpen) {
          refreshFilterFromInput();
        } else {
          setActiveIndex(state.activeIndex - 1);
        }
      } else if (e.key === "Enter") {
        if (state.listOpen && state.activeIndex >= 0 && state.filtered[state.activeIndex]) {
          e.preventDefault();
          pickSymbol(state.filtered[state.activeIndex]);
        } else {
          // Exact match on current text, or load whatever is in hidden field
          const typed = input.value.trim().toUpperCase();
          const hit = state.allSymbols.find((s) => s.symbol.toUpperCase() === typed);
          if (hit) {
            e.preventDefault();
            pickSymbol(hit);
          } else if ($("symbol").value) {
            e.preventDefault();
            closeList();
            loadChart();
          }
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        // Restore display to selected symbol
        input.value = $("symbol").value || "";
        closeList();
        input.blur();
      }
    });

    document.addEventListener("mousedown", (e) => {
      if (!combo.contains(e.target)) closeList();
    });
  }

  async function loadSymbols() {
    let items = GOLDEN.map((s) => ({ symbol: s, market: inferMarket(s) }));
    try {
      const data = await apiGet("/symbols");
      if (data.symbols && data.symbols.length) {
        items = data.symbols.map((s) => ({
          symbol: s.symbol,
          market: s.market || inferMarket(s.symbol),
        }));
        // ensure golden present
        GOLDEN.forEach((g) => {
          if (!items.some((x) => x.symbol === g)) {
            items.push({ symbol: g, market: inferMarket(g) });
          }
        });
      }
    } catch (_) {
      // fall back to golden
    }

    // Stable sort: market then symbol
    items.sort((a, b) => {
      const ma = String(a.market || "");
      const mb = String(b.market || "");
      return ma.localeCompare(mb) || a.symbol.localeCompare(b.symbol);
    });
    state.allSymbols = items;

    // Default selection: AAPL if present, else first
    const preferred =
      items.find((x) => x.symbol === "AAPL") ||
      items.find((x) => x.symbol === GOLDEN[0]) ||
      items[0];
    if (preferred) {
      pickSymbol(preferred, { silent: true });
    }
  }

  async function loadChart() {
    ensureCharts();
    const symbol = $("symbol").value;
    if (!symbol) {
      setStatus("Pick a symbol first", "warn");
      return;
    }
    const timeframe = $("timeframe").value;
    const from = $("from").value || null;
    const to = $("to").value || null;
    const key = $("apiKey").value.trim();
    if (key) localStorage.setItem("dixon_api_key", key);

    setStatus(`Loading ${symbol} ${timeframe}…`);

    try {
      const bq = new URLSearchParams({ symbol, timeframe });
      if (from) bq.set("from", from);
      if (to) bq.set("to", to);
      const barsPayload = await apiGet(`/bars?${bq}`);
      const bars = barsPayload.bars || [];
      if (!bars.length) {
        clearPriceOverlays();
        state.candleSeries.setData([]);
        state.volumeSeries.setData([]);
        state.rsiSeries.setData([]);
        state.macdLine.setData([]);
        state.macdSignal.setData([]);
        state.macdHist.setData([]);
        setStatus(`No bars for ${symbol} (${timeframe}). Ingest data first.`, "warn");
        return;
      }

      const candleData = bars.map((b) => ({
        time: businessDay(b.date),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }));
      const volData = bars.map((b) => ({
        time: businessDay(b.date),
        value: b.volume || 0,
        color: b.close >= b.open ? "rgba(38,166,154,0.4)" : "rgba(239,83,80,0.4)",
      }));
      const orderedDates = bars.map((b) => b.date);
      state.barDates = new Set(orderedDates);

      const indNames = selectedIndicators();
      const showRsi = paneEnabled("rsi");
      const showMacd = paneEnabled("macd");
      $("rsi-chart").style.display = showRsi ? "" : "none";
      $("macd-chart").style.display = showMacd ? "" : "none";

      const toFetch = indNames.slice();
      if (showRsi) toFetch.push("RSI_14");
      if (showMacd) {
        toFetch.push("MACD_12_26_9", "MACD_SIGNAL_12_26_9", "MACD_HIST_12_26_9");
      }

      const fetched = toFetch.length
        ? await fetchIndicators(symbol, toFetch, from, to, timeframe)
        : { series: {} };
      const byName = fetched.series || {};

      // setData publishes an intermediate range. Hold sync until fitContent so
      // that range cannot replace the fitted one on the other panes.
      syncingTime = true;
      try {
        state.candleSeries.setData(candleData);
        state.volumeSeries.setData(volData);
        clearPriceOverlays();
        indNames.forEach((n) => {
          const aligned = alignSeries(byName[n], state.barDates);
          if (n.startsWith("BB_")) {
            addLine(n, aligned, IND_COLORS[n], {
              lineStyle: n === "BB_MID_20_2" ? 0 : 2,
              lineWidth: n === "BB_MID_20_2" ? 1 : 1,
            });
          } else {
            addLine(n, aligned, IND_COLORS[n]);
          }
        });
        if (showRsi) {
          state.rsiSeries.setData(panePoints(byName.RSI_14, orderedDates));
        } else {
          state.rsiSeries.setData([]);
          state.rsiChart.timeScale().resetTimeScale();
        }

        if (showMacd) {
          const macd = panePoints(byName.MACD_12_26_9, orderedDates);
          const sig = panePoints(byName.MACD_SIGNAL_12_26_9, orderedDates);
          const histRaw = panePoints(byName.MACD_HIST_12_26_9, orderedDates);
          state.macdLine.setData(macd);
          state.macdSignal.setData(sig);
          state.macdHist.setData(
            histRaw.map((p) =>
              p.value == null
                ? p
                : {
                    ...p,
                    color: p.value >= 0 ? "rgba(38,166,154,0.7)" : "rgba(239,83,80,0.7)",
                  }
            )
          );
        } else {
          state.macdLine.setData([]);
          state.macdSignal.setData([]);
          state.macdHist.setData([]);
          state.macdChart.timeScale().resetTimeScale();
        }

        resizeCharts();
      } finally {
        syncingTime = false;
      }
      fitPanes();

      setStatus(
        `${symbol} ${timeframe}: ${bars.length} bars · ${indNames.length} price overlays`
      );
    } catch (err) {
      console.error(err);
      setStatus(String(err.message || err), "error");
    }
  }

  /* ---------- Top-50 + strategies ---------- */

  function setHint(id, msg, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || "";
    el.className = "hint" + (kind ? " " + kind : "");
  }

  function fmtNum(value, digits) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
  }

  function fmtPct(value) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return (Number(value) * 100).toFixed(2) + "%";
  }

  async function loadScreener() {
    const marketEl = $("scrMarket");
    const table = $("scrTable");
    const dayEl = $("scrDate");
    if (!marketEl || !table) {
      setHint("scrStatus", "Top 50 controls are not on this page", "error");
      return;
    }
    const market = marketEl.value;
    const day = dayEl ? dayEl.value : "";
    const q = new URLSearchParams({ market });
    if (day) q.set("date", day);
    setHint("scrStatus", "Loading…");
    try {
      const data = await apiGet(`/screener/top50?${q}`);
      if (data.date && dayEl && !dayEl.value) dayEl.value = data.date;
      const body = table.querySelector("tbody");
      body.innerHTML = "";
      (data.rows || []).forEach((row) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
          `<td>${row.rank}</td>` +
          `<td>${esc(row.symbol)}</td>` +
          `<td>${fmtNum(row.close, 4)}</td>` +
          `<td>${fmtNum(row.turnover, 0)}</td>` +
          `<td>${fmtPct(row.day_gain)}</td>` +
          `<td class="${row.spike ? "spike-yes" : "spike-no"}">${row.spike ? "spike" : ""}</td>`;
        tr.addEventListener("click", () => {
          const item =
            state.allSymbols.find((s) => s.symbol === row.symbol) ||
            { symbol: row.symbol, market: data.market };
          pickSymbol(item);
        });
        body.appendChild(tr);
      });
      setHint(
        "scrStatus",
        `${data.date || "—"} · ${data.count || 0} names · ${data.spike_count || 0} spikes`
      );
    } catch (err) {
      setHint("scrStatus", String(err.message || err), "error");
    }
  }

  function emptyRules() {
    return {
      long: { logic: "AND", conditions: [{ type: "price_gt_ma", window: 20 }] },
      flat: { logic: "OR", conditions: [{ type: "price_lt_ma", window: 20 }] },
      short: { logic: "AND", conditions: [] },
    };
  }

  function conditionNeedsWindow(type) {
    return type === "price_gt_ma" || type === "price_lt_ma" || type === "ma_slope_gt_0" || type === "ma_slope_lt_0";
  }

  function isRsiType(type) {
    return type === "rsi_lte" || type === "rsi_gte";
  }

  function isDonchianType(type) {
    return type === "donchian_high" || type === "donchian_low";
  }

  function isRocType(type) {
    return type === "roc_gte" || type === "roc_lte";
  }

  function formatStack(cond) {
    const op = (cond.direction || "up") === "down" ? "<" : ">";
    return (cond.windows || []).join(op);
  }

  function parseStack(text) {
    const raw = String(text || "").replace(/\s+/g, "");
    if (!raw) throw new Error("MA stack is empty");
    const parts = raw.split(/(>|<)/);
    if (parts.length < 3) throw new Error("Stack needs at least two MAs, e.g. 5>20>50");
    let direction = null;
    const windows = [];
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) {
        const n = Number(parts[i]);
        if (!MA_MENU.includes(n)) throw new Error(`MA ${parts[i]} is not in the fixed menu`);
        windows.push(n);
      } else {
        const dir = parts[i] === ">" ? "up" : "down";
        if (direction && direction !== dir) throw new Error("Use only > or only < in a stack");
        direction = dir;
      }
    }
    return { windows, direction: direction || "up" };
  }

  function renderRules(rules) {
    const root = $("rulesEditor");
    root.innerHTML = "";
    ["long", "flat", "short"].forEach((side) => {
      const group = (rules && rules[side]) || { logic: "AND", conditions: [] };
      const box = document.createElement("div");
      box.className = "rules-side";
      const title = document.createElement("h3");
      title.innerHTML =
        `<span>${side}</span>` +
        `<select data-logic="${side}">` +
        `<option value="AND"${group.logic === "AND" ? " selected" : ""}>AND</option>` +
        `<option value="OR"${group.logic === "OR" ? " selected" : ""}>OR</option>` +
        `</select>` +
        `<button type="button" class="secondary" data-add="${side}">Add condition</button>`;
      box.appendChild(title);
      (group.conditions || []).forEach((cond) => {
        const row = document.createElement("div");
        row.className = "cond";
        row.dataset.side = side;
        const typeOpts = COND_TYPES.map(
          ([id, label]) => `<option value="${id}"${cond.type === id ? " selected" : ""}>${label}</option>`
        ).join("");
        const winOpts = MA_MENU.map(
          (w) => `<option value="${w}"${Number(cond.window) === w ? " selected" : ""}>${w}</option>`
        ).join("");
        const lookback = isRsiType(cond.type) || isRocType(cond.type)
          ? cond.period
          : (isDonchianType(cond.type) || cond.type === "rel_volume" ? cond.window : "");
        const showThreshold = isRsiType(cond.type) || isRocType(cond.type);
        row.innerHTML =
          `<select class="cond-type">${typeOpts}</select>` +
          `<select class="cond-window">${winOpts}</select>` +
          `<input class="cond-stack" type="text" placeholder="5>20>50" value="${esc(cond.type === "ma_stack" ? formatStack(cond) : "")}" />` +
          `<input class="cond-k" type="number" step="0.1" min="0.1" placeholder="k" title="Volume multiple k" value="${esc(cond.type === "rel_volume" ? cond.k : "")}" />` +
          `<input class="cond-n" type="number" step="1" min="1" placeholder="N" title="Lookback N, RSI period, or ROC period" value="${esc(lookback == null ? "" : lookback)}" />` +
          `<input class="cond-threshold" type="number" step="0.1" placeholder="threshold" title="RSI or ROC threshold" value="${esc(showThreshold ? cond.threshold : "")}" />` +
          `<button type="button" class="secondary cond-del">Remove</button>`;
        box.appendChild(row);
        syncCondRow(row, false);
      });
      root.appendChild(box);
    });
  }

  function syncCondRow(row, fillDefaults) {
    const type = row.querySelector(".cond-type").value;
    const isRel = type === "rel_volume";
    const isRsi = isRsiType(type);
    const isDon = isDonchianType(type);
    const isRoc = isRocType(type);
    row.querySelector(".cond-window").hidden = !conditionNeedsWindow(type);
    row.querySelector(".cond-stack").hidden = type !== "ma_stack";
    row.querySelector(".cond-k").hidden = !isRel;
    row.querySelector(".cond-n").hidden = !(isRel || isRsi || isDon || isRoc);
    const threshold = row.querySelector(".cond-threshold");
    threshold.hidden = !(isRsi || isRoc);
    if (isRoc) {
      threshold.min = "-100";
      threshold.max = "10000";
      threshold.step = "0.1";
      threshold.placeholder = "±%";
      threshold.title = "ROC threshold in percent points. 1 means +1%.";
    } else {
      threshold.min = "0";
      threshold.max = "100";
      threshold.step = "0.1";
      threshold.placeholder = "threshold";
      threshold.title = "RSI threshold";
    }
    const n = row.querySelector(".cond-n");
    if (isRel) n.placeholder = "N";
    else if (isRsi || isRoc) n.placeholder = "period";
    else if (type === "donchian_high") n.placeholder = "N";
    else if (type === "donchian_low") n.placeholder = "M";
    if (!fillDefaults) return;
    const defaults = PREDICATE_DEFAULTS[type];
    if (!defaults) return;
    if (defaults.k != null) row.querySelector(".cond-k").value = defaults.k;
    if (defaults.window != null) n.value = defaults.window;
    if (defaults.period != null) n.value = defaults.period;
    if (defaults.threshold != null) row.querySelector(".cond-threshold").value = defaults.threshold;
  }

  function readRequiredNumber(input, label) {
    const raw = String(input.value == null ? "" : input.value).trim();
    if (raw === "") throw new Error(`${label} is required`);
    const n = Number(raw);
    if (!Number.isFinite(n)) throw new Error(`${label} must be a number`);
    return n;
  }

  function readRequiredInt(input, label) {
    const n = readRequiredNumber(input, label);
    if (!Number.isInteger(n)) throw new Error(`${label} must be a whole number`);
    return n;
  }

  function readRulesFromDom() {
    const rules = {};
    ["long", "flat", "short"].forEach((side) => {
      const logic = document.querySelector(`[data-logic="${side}"]`).value;
      const conditions = [];
      document.querySelectorAll(`.cond[data-side="${side}"]`).forEach((row) => {
        const type = row.querySelector(".cond-type").value;
        if (type === "top50_spike") {
          conditions.push({ type });
        } else if (type === "ma_stack") {
          const parsed = parseStack(row.querySelector(".cond-stack").value);
          conditions.push({ type, windows: parsed.windows, direction: parsed.direction });
        } else if (type === "rel_volume") {
          conditions.push({
            type,
            k: readRequiredNumber(row.querySelector(".cond-k"), "Relative volume k"),
            window: readRequiredInt(row.querySelector(".cond-n"), "Relative volume N"),
          });
        } else if (isRsiType(type)) {
          conditions.push({
            type,
            period: readRequiredInt(row.querySelector(".cond-n"), "RSI period"),
            threshold: readRequiredNumber(row.querySelector(".cond-threshold"), "RSI threshold"),
          });
        } else if (isDonchianType(type)) {
          const label = type === "donchian_high" ? "Donchian N" : "Donchian M";
          conditions.push({ type, window: readRequiredInt(row.querySelector(".cond-n"), label) });
        } else if (isRocType(type)) {
          conditions.push({
            type,
            period: readRequiredInt(row.querySelector(".cond-n"), "ROC period"),
            threshold: readRequiredNumber(row.querySelector(".cond-threshold"), "ROC threshold"),
          });
        } else {
          conditions.push({ type, window: Number(row.querySelector(".cond-window").value) });
        }
      });
      rules[side] = { logic, conditions };
    });
    const trail = readTrailPct();
    if (trail != null) rules.trail_pct = trail;
    return rules;
  }

  function readTrailPct() {
    const input = $("stTrailPct");
    if (!input) return null;
    const raw = String(input.value == null ? "" : input.value).trim();
    if (raw === "") return null;
    const n = Number(raw);
    if (!Number.isFinite(n)) throw new Error("Trail % must be a number");
    return n;
  }

  function setTrailPct(value) {
    const input = $("stTrailPct");
    if (!input) return;
    input.value = value == null || value === "" ? "" : String(value);
  }

  // Street presets. `description` is the hover/focus tooltip; `rules` is what
  // the button fills. A later preset can carry the same fields and render itself.
  const PUBLIC_PATTERNS = [
    {
      id: "relvol",
      label: "Volume breakout",
      description:
        "Relative-volume spike. Long when volume is at least 1.5× SMA(volume, 20) and close is above MA20. Exit when close is below MA20.",
      filled: "Volume breakout filled (k=1.5, N=20). Save to keep it.",
      rules: {
        long: {
          logic: "AND",
          conditions: [
            { type: "price_gt_ma", window: 20 },
            { type: "rel_volume", k: 1.5, window: 20 },
          ],
        },
        flat: { logic: "OR", conditions: [{ type: "price_lt_ma", window: 20 }] },
        short: { logic: "AND", conditions: [] },
      },
    },
    {
      id: "rsi",
      label: "Connors RSI(2)",
      description:
        "Mean reversion. Long when RSI(2) is 5 or below. Exit when close is above MA5, or when RSI(2) is 70 or above.",
      filled: "Connors RSI(2) filled (entry ≤ 5, exit SMA(5) or RSI ≥ 70). Save to keep it.",
      rules: {
        long: { logic: "AND", conditions: [{ type: "rsi_lte", period: 2, threshold: 5 }] },
        flat: {
          logic: "OR",
          conditions: [
            { type: "price_gt_ma", window: 5 },
            { type: "rsi_gte", period: 2, threshold: 70 },
          ],
        },
        short: { logic: "AND", conditions: [] },
      },
    },
    {
      id: "donchian",
      label: "Donchian 20/10",
      description:
        "Channel breakout. Long when close is above the prior 20-bar high. Exit when close is below the prior 10-bar low.",
      filled: "Donchian 20/10 filled. Save to keep it.",
      rules: {
        long: { logic: "AND", conditions: [{ type: "donchian_high", window: 20 }] },
        flat: { logic: "OR", conditions: [{ type: "donchian_low", window: 10 }] },
        short: { logic: "AND", conditions: [] },
      },
    },
    {
      id: "roc",
      label: "ROC(7) ±1%",
      description:
        "Momentum. Long when ROC(7) is at least +1%. Short when ROC(7) is −1% or lower. A 5% trailing stop exits after a 5% move off the peak high (long) or trough low (short) since entry. The stop fills at the next open. Add a relative-volume, RSI, Donchian, or MA condition on the same side to require it as well.",
      filled: "ROC(7) ±1% filled with a 5% trail. Save to keep it.",
      trail_pct: 5,
      rules: {
        long: { logic: "AND", conditions: [{ type: "roc_gte", period: 7, threshold: 1 }] },
        flat: { logic: "AND", conditions: [] },
        short: { logic: "AND", conditions: [{ type: "roc_lte", period: 7, threshold: -1 }] },
      },
    },
  ];

  function patternById(which) {
    for (let i = 0; i < PUBLIC_PATTERNS.length; i++) {
      if (PUBLIC_PATTERNS[i].id === which) return PUBLIC_PATTERNS[i];
    }
    return null;
  }

  function patternTooltip(pattern) {
    return (pattern && (pattern.description || pattern.tooltip)) || "";
  }

  function presetRules(which) {
    const pattern = patternById(which) || patternById("donchian");
    return JSON.parse(JSON.stringify(pattern.rules));
  }

  function renderPublicPatterns() {
    const root = $("strategyPresets");
    if (!root) return;
    root.querySelectorAll("[data-preset]").forEach((el) => el.remove());
    let descRoot = root.querySelector(".preset-desc-root");
    if (!descRoot) {
      descRoot = document.createElement("div");
      descRoot.className = "preset-desc-root";
      root.appendChild(descRoot);
    }
    descRoot.replaceChildren();
    PUBLIC_PATTERNS.forEach((pattern) => {
      const text = patternTooltip(pattern);
      const descId = "preset-desc-" + pattern.id;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary";
      btn.setAttribute("data-preset", pattern.id);
      btn.setAttribute("data-description", text);
      if (text) btn.setAttribute("aria-describedby", descId);
      btn.textContent = pattern.label;
      root.insertBefore(btn, descRoot);
      if (!text) return;
      const desc = document.createElement("span");
      desc.id = descId;
      desc.textContent = text;
      descRoot.appendChild(desc);
    });
  }

  function bindPresetTooltips(presets) {
    if (presets.dataset.tips === "1") return;
    presets.dataset.tips = "1";
    const tip = document.createElement("div");
    tip.id = "presetTooltip";
    tip.className = "preset-tooltip";
    tip.setAttribute("aria-hidden", "true");
    tip.hidden = true;
    document.body.appendChild(tip);

    function buttonFrom(node) {
      if (!node || !node.closest) return null;
      const btn = node.closest("[data-preset]");
      return btn && presets.contains(btn) ? btn : null;
    }

    function hide() {
      tip.hidden = true;
    }

    function show(btn) {
      const text = btn.getAttribute("data-description") || "";
      if (!text) {
        hide();
        return;
      }
      tip.textContent = text;
      tip.hidden = false;
      tip.style.visibility = "hidden";
      const rect = btn.getBoundingClientRect();
      const maxWidth = Math.min(352, window.innerWidth - 16);
      tip.style.maxWidth = maxWidth + "px";
      let left = rect.left;
      if (left + tip.offsetWidth > window.innerWidth - 8) {
        left = window.innerWidth - tip.offsetWidth - 8;
      }
      if (left < 8) left = 8;
      let top = rect.bottom + 6;
      if (top + tip.offsetHeight > window.innerHeight - 8) {
        top = rect.top - tip.offsetHeight - 6;
      }
      if (top < 8) top = 8;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
      tip.style.visibility = "visible";
    }

    presets.addEventListener("mouseover", (e) => {
      const btn = buttonFrom(e.target);
      if (btn) show(btn);
    });
    presets.addEventListener("mouseout", (e) => {
      const btn = buttonFrom(e.target);
      if (!btn || buttonFrom(e.relatedTarget) !== btn) hide();
    });
    presets.addEventListener("focusin", (e) => {
      const btn = buttonFrom(e.target);
      if (btn) show(btn);
    });
    presets.addEventListener("focusout", (e) => {
      if (buttonFrom(e.target) && buttonFrom(e.relatedTarget) !== buttonFrom(e.target)) hide();
    });
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  }

  function renderStrategyList() {
    const ul = $("strategyList");
    ul.innerHTML = "";
    if (!state.strategies.length) {
      const li = document.createElement("li");
      li.textContent = "No strategies yet";
      ul.appendChild(li);
      return;
    }
    state.strategies.forEach((st) => {
      const li = document.createElement("li");
      if (st.id === state.editingId) li.className = "active";
      li.innerHTML = `<div>${esc(st.name)}</div><div class="sym">${esc(st.symbol)} · ${esc(st.timeframe)}</div>`;
      li.addEventListener("click", () => loadStrategyIntoForm(st));
      ul.appendChild(li);
    });
  }

  function loadStrategyIntoForm(st) {
    state.editingId = st.id;
    $("stName").value = st.name;
    $("stSymbol").value = st.symbol;
    $("stTimeframe").value = st.timeframe;
    renderRules(st.rules);
    setTrailPct(st.rules && st.rules.trail_pct != null ? st.rules.trail_pct : "");
    renderStrategyList();
    setHint("stStatus", `Editing ${st.name}`);
  }

  function resetStrategyForm() {
    state.editingId = null;
    $("stName").value = "";
    $("stSymbol").value = $("symbol").value || "";
    $("stTimeframe").value = $("timeframe").value || "daily";
    renderRules(emptyRules());
    setTrailPct("");
    renderStrategyList();
    setHint("stStatus", "New strategy");
  }

  async function reloadStrategies() {
    const data = await apiGet("/strategies");
    state.strategies = data.strategies || [];
    renderStrategyList();
    return data;
  }

  function readCostOverrides() {
    const map = [
      ["cHkBps", "hk_commission_bps"],
      ["cHkTicks", "hk_spread_ticks"],
      ["cUsShare", "us_commission_per_share"],
      ["cUsMin", "us_commission_min"],
      ["cUsBps", "us_spread_bps"],
      ["cFrac", "equity_fraction"],
    ];
    const costs = {};
    map.forEach(([id, key]) => {
      const raw = $(id).value.trim();
      if (raw !== "") costs[key] = Number(raw);
    });
    return costs;
  }

  function renderBacktest(result) {
    const m = result.metrics || {};
    const bits = [
      ["Ending equity", `${fmtNum(m.ending_equity, 2)} ${result.currency || ""}`],
      ["Total P&L", fmtNum(m.total_pnl, 2)],
      ["Return", fmtPct(m.total_return)],
      ["Trades", String(m.trade_count ?? 0)],
      ["Win rate", m.win_rate == null ? "—" : `${fmtPct(m.win_rate)} (target > 50%)`],
      ["Max DD", `${fmtPct(m.max_drawdown)} (target ≤ 10%)`],
    ];
    $("btMetrics").innerHTML = bits
      .map((pair) => `<div class="metric"><span class="k">${esc(pair[0])}</span><span class="v">${esc(pair[1])}</span></div>`)
      .join("");
    const body = $("btTrades").querySelector("tbody");
    body.innerHTML = "";
    (result.trades || []).forEach((t) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${esc(t.side)}</td>` +
        `<td>${esc(t.entry_date)}</td>` +
        `<td>${esc(t.exit_reason && t.exit_reason !== "signal" ? t.exit_date + " · " + t.exit_reason : t.exit_date)}</td>` +
        `<td>${fmtNum(t.shares, 4)}</td>` +
        `<td>${fmtNum(t.entry_price, 4)}</td>` +
        `<td>${fmtNum(t.exit_price, 4)}</td>` +
        `<td>${fmtNum(t.pnl, 2)}</td>`;
      body.appendChild(tr);
    });
    if (result.open_position) {
      const op = result.open_position;
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${esc(op.side)} open</td>` +
        `<td>${esc(op.entry_date)}</td>` +
        `<td>mark ${fmtNum(op.mark, 4)}</td>` +
        `<td>${fmtNum(op.shares, 4)}</td>` +
        `<td>${fmtNum(op.entry_price, 4)}</td>` +
        `<td></td>` +
        `<td>${fmtNum(op.unrealized_pnl, 2)}</td>`;
      body.appendChild(tr);
    }
    drawEquity(result.equity_curve || []);
  }

  function drawEquity(curve) {
    const el = $("equity-chart");
    if (state.equityChart) {
      state.equityChart.remove();
      state.equityChart = null;
    }
    if (!curve.length || typeof LightweightCharts === "undefined") return;
    const chart = LightweightCharts.createChart(el, {
      ...chartOpts(el.clientHeight || 180),
      height: 180,
      width: el.clientWidth || 640,
    });
    const series = chart.addLineSeries({ color: "#3b82f6", lineWidth: 2, priceLineVisible: false });
    series.setData(curve.map((pt) => ({ time: businessDay(pt.date), value: pt.equity })));
    chart.timeScale().fitContent();
    state.equityChart = chart;
  }

  function bindScreener() {
    const btn = $("btnScreener");
    if (!btn || btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      loadScreener().catch((err) => {
        console.error(err);
        setHint("scrStatus", String(err.message || err), "error");
      });
    });
  }

  function wireResearch() {
    document.querySelectorAll(".research-tabs .tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".research-tabs .tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const which = btn.getAttribute("data-tab");
        $("tab-screener").hidden = which !== "screener";
        $("tab-strategy").hidden = which !== "strategy";
        if (which === "strategy" && state.equityChart) {
          state.equityChart.applyOptions({ width: $("equity-chart").clientWidth });
        }
      });
    });
    bindScreener();
    $("rulesEditor").addEventListener("change", (e) => {
      const row = e.target.closest(".cond");
      if (row && e.target.classList.contains("cond-type")) syncCondRow(row, true);
    });
    const presets = $("strategyPresets");
    if (presets) {
      renderPublicPatterns();
      bindPresetTooltips(presets);
      presets.addEventListener("click", (e) => {
        const btn = e.target.closest && e.target.closest("[data-preset]");
        const which = btn && presets.contains(btn) ? btn.getAttribute("data-preset") : "";
        if (!which) return;
        renderRules(presetRules(which));
        const pattern = patternById(which);
        setTrailPct(pattern && pattern.trail_pct != null ? pattern.trail_pct : "");
        setHint("stStatus", (pattern && pattern.filled) || "Preset filled");
      });
    }
    $("rulesEditor").addEventListener("click", (e) => {
      const add = e.target.getAttribute && e.target.getAttribute("data-add");
      if (add) {
        let rules;
        try {
          rules = readRulesFromDom();
        } catch (err) {
          setHint("stStatus", String(err.message || err), "error");
          return;
        }
        rules[add].conditions.push({ type: "price_gt_ma", window: 20 });
        renderRules(rules);
        return;
      }
      if (e.target.classList.contains("cond-del")) {
        const row = e.target.closest(".cond");
        const side = row.dataset.side;
        const rules = readRulesFromDom();
        const rows = Array.from(document.querySelectorAll(`.cond[data-side="${side}"]`));
        const idx = rows.indexOf(row);
        if (idx >= 0) rules[side].conditions.splice(idx, 1);
        renderRules(rules);
      }
    });
    $("btnNewStrategy").addEventListener("click", resetStrategyForm);
    $("btnReloadStrategies").addEventListener("click", () => {
      reloadStrategies().catch((err) => setHint("stStatus", String(err.message || err), "error"));
    });
    $("btnUseChartSymbol").addEventListener("click", () => {
      $("stSymbol").value = $("symbol").value || "";
      $("stTimeframe").value = $("timeframe").value || "daily";
    });
    $("strategyForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      let rules;
      try {
        rules = readRulesFromDom();
      } catch (err) {
        setHint("stStatus", String(err.message || err), "error");
        return;
      }
      const payload = {
        name: $("stName").value.trim(),
        symbol: $("stSymbol").value.trim(),
        timeframe: $("stTimeframe").value,
        rules,
      };
      try {
        const saved = state.editingId
          ? await apiSend("PUT", `/strategies/${state.editingId}`, payload)
          : await apiSend("POST", "/strategies", payload);
        state.editingId = saved.id;
        await reloadStrategies();
        loadStrategyIntoForm(saved);
        setHint("stStatus", "Saved");
      } catch (err) {
        setHint("stStatus", String(err.message || err), "error");
      }
    });
    $("btnDeleteStrategy").addEventListener("click", async () => {
      if (!state.editingId) return;
      if (!window.confirm("Delete this strategy?")) return;
      try {
        await apiSend("DELETE", `/strategies/${state.editingId}`);
        resetStrategyForm();
        await reloadStrategies();
        setHint("stStatus", "Deleted");
      } catch (err) {
        setHint("stStatus", String(err.message || err), "error");
      }
    });
    $("btnBacktest").addEventListener("click", async () => {
      if (!state.editingId) {
        setHint("stStatus", "Save the strategy before running a backtest", "error");
        return;
      }
      const start = $("btStart").value;
      const end = $("btEnd").value;
      if (!start || !end) {
        setHint("stStatus", "Pick a start and end date", "error");
        return;
      }
      const body = { start, end };
      const costs = readCostOverrides();
      if (Object.keys(costs).length) body.costs = costs;
      setHint("stStatus", "Running backtest…");
      try {
        const result = await apiSend("POST", `/strategies/${state.editingId}/backtest`, body);
        renderBacktest(result);
        const m = result.metrics;
        setHint(
          "stStatus",
          `${m.trade_count} trades · P&L ${fmtNum(m.total_pnl, 2)} ${result.currency}`
        );
      } catch (err) {
        setHint("stStatus", String(err.message || err), "error");
      }
    });
    renderRules(emptyRules());
  }

  function wire() {
    $("btnLoad").addEventListener("click", loadChart);
    $("btnResetRange").addEventListener("click", () => {
      $("from").value = "";
      $("to").value = "";
      loadChart();
    });
    wireSymbolPicker();
    $("timeframe").addEventListener("change", loadChart);
    document.querySelectorAll(".overlays input").forEach((cb) => {
      cb.addEventListener("change", loadChart);
    });
    wireResearch();
    const saved = localStorage.getItem("dixon_api_key");
    if (saved) $("apiKey").value = saved;
  }

  async function boot() {
    // Bind and fetch Top 50 before chart setup. A throw or a hung loadChart()
    // must not leave #btnScreener without a listener or skip /screener/top50.
    bindScreener();
    const screenerTask = loadScreener().catch((err) => {
      console.error(err);
      setHint("scrStatus", String(err.message || err), "error");
    });
    try {
      wire();
      ensureCharts();
      await loadSymbols();
      await loadChart();
    } catch (err) {
      console.error(err);
      if ($("status")) setStatus(String(err.message || err), "error");
    }
    try {
      await reloadStrategies();
    } catch (_) {
      /* strategies stay empty until the API is reachable */
    }
    await screenerTask;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

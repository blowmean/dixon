/* Project Dixon charts UI — lightweight-charts + /bars + /indicators */
(function () {
  "use strict";

  const GOLDEN = ["0700.HK", "0005.HK", "AAPL", "MSFT", "SPY"];
  const MAX_RESULTS = 50;

  const IND_COLORS = {
    MA_20: "#f59e0b",
    MA_50: "#3b82f6",
    MA_200: "#a855f7",
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
    state.volumeSeries = state.priceChart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    state.priceChart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
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

    const charts = [state.priceChart, state.rsiChart, state.macdChart];
    charts.forEach((src) => {
      src.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (!range) return;
        charts.forEach((c) => {
          if (c !== src) c.timeScale().setVisibleLogicalRange(range);
        });
      });
    });

    window.addEventListener("resize", resizeCharts);
    resizeCharts();
  }

  function resizeCharts() {
    const main = document.querySelector("main");
    if (!main || !state.priceChart) return;
    const showRsi = $("rsi-chart").style.display !== "none";
    const showMacd = $("macd-chart").style.display !== "none";
    const w = main.clientWidth;
    const h = main.clientHeight;
    let panes = 1 + (showRsi ? 1 : 0) + (showMacd ? 1 : 0);
    const priceH = Math.floor(h * (panes === 1 ? 1 : panes === 2 ? 0.7 : 0.55));
    const otherH = Math.floor((h - priceH) / Math.max(1, panes - 1));
    state.priceChart.applyOptions({ width: w, height: priceH });
    if (showRsi) state.rsiChart.applyOptions({ width: w, height: otherH });
    if (showMacd) state.macdChart.applyOptions({ width: w, height: otherH });
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

  async function fetchIndicator(symbol, name, from, to) {
    const q = new URLSearchParams({ symbol, name });
    if (from) q.set("from", from);
    if (to) q.set("to", to);
    return apiGet(`/indicators?${q}`);
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
      state.candleSeries.setData(candleData);
      state.volumeSeries.setData(volData);
      state.barDates = new Set(bars.map((b) => b.date));

      let indFrom = from;
      let indTo = to;
      if (timeframe !== "daily" && bars.length) {
        indFrom = bars[0].period_start || bars[0].date;
        indTo = bars[bars.length - 1].date;
      }

      clearPriceOverlays();
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

      const results = await Promise.all(
        toFetch.map((n) => fetchIndicator(symbol, n, indFrom, indTo).then((r) => [n, r]))
      );
      const byName = Object.fromEntries(results);

      indNames.forEach((n) => {
        const aligned = alignSeries(byName[n]?.series, state.barDates);
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
        state.rsiSeries.setData(alignSeries(byName.RSI_14?.series, state.barDates));
      } else {
        state.rsiSeries.setData([]);
      }

      if (showMacd) {
        const macd = alignSeries(byName.MACD_12_26_9?.series, state.barDates);
        const sig = alignSeries(byName.MACD_SIGNAL_12_26_9?.series, state.barDates);
        const histRaw = alignSeries(byName.MACD_HIST_12_26_9?.series, state.barDates);
        state.macdLine.setData(macd);
        state.macdSignal.setData(sig);
        state.macdHist.setData(
          histRaw.map((p) => ({
            ...p,
            color: p.value >= 0 ? "rgba(38,166,154,0.7)" : "rgba(239,83,80,0.7)",
          }))
        );
      } else {
        state.macdLine.setData([]);
        state.macdSignal.setData([]);
        state.macdHist.setData([]);
      }

      state.priceChart.timeScale().fitContent();
      if (showRsi) state.rsiChart.timeScale().fitContent();
      if (showMacd) state.macdChart.timeScale().fitContent();
      resizeCharts();

      const note =
        timeframe === "daily"
          ? ""
          : " · overlays = daily cache aligned to period_end";
      setStatus(
        `${symbol} ${timeframe}: ${bars.length} bars · ${indNames.length} price overlays${note}`
      );
    } catch (err) {
      console.error(err);
      setStatus(String(err.message || err), "error");
    }
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
    const saved = localStorage.getItem("dixon_api_key");
    if (saved) $("apiKey").value = saved;
  }

  async function boot() {
    wire();
    ensureCharts();
    await loadSymbols();
    await loadChart();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

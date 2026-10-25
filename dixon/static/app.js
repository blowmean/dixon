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
  };

  const MA_MENU = [5, 9, 20, 30, 50, 100, 250];
  const COND_TYPES = [
    ["price_gt_ma", "Price > MA"],
    ["price_lt_ma", "Price < MA"],
    ["ma_slope_gt_0", "MA slope > 0"],
    ["ma_slope_lt_0", "MA slope < 0"],
    ["ma_stack", "MA stack"],
    ["top50_spike", "In Top-50 spike"],
  ];

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
      state.candleSeries.setData(candleData);
      state.volumeSeries.setData(volData);
      state.barDates = new Set(bars.map((b) => b.date));

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

      const fetched = toFetch.length
        ? await fetchIndicators(symbol, toFetch, from, to, timeframe)
        : { series: {} };
      const byName = fetched.series || {};

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
        state.rsiSeries.setData(alignSeries(byName.RSI_14, state.barDates));
      } else {
        state.rsiSeries.setData([]);
      }

      if (showMacd) {
        const macd = alignSeries(byName.MACD_12_26_9, state.barDates);
        const sig = alignSeries(byName.MACD_SIGNAL_12_26_9, state.barDates);
        const histRaw = alignSeries(byName.MACD_HIST_12_26_9, state.barDates);
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
        row.innerHTML =
          `<select class="cond-type">${typeOpts}</select>` +
          `<select class="cond-window">${winOpts}</select>` +
          `<input class="cond-stack" type="text" placeholder="5>20>50" value="${esc(cond.type === "ma_stack" ? formatStack(cond) : "")}" />` +
          `<button type="button" class="secondary cond-del">Remove</button>`;
        box.appendChild(row);
        syncCondRow(row);
      });
      root.appendChild(box);
    });
  }

  function syncCondRow(row) {
    const type = row.querySelector(".cond-type").value;
    row.querySelector(".cond-window").hidden = !conditionNeedsWindow(type);
    row.querySelector(".cond-stack").hidden = type !== "ma_stack";
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
        } else {
          conditions.push({ type, window: Number(row.querySelector(".cond-window").value) });
        }
      });
      rules[side] = { logic, conditions };
    });
    return rules;
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
    renderStrategyList();
    setHint("stStatus", `Editing ${st.name}`);
  }

  function resetStrategyForm() {
    state.editingId = null;
    $("stName").value = "";
    $("stSymbol").value = $("symbol").value || "";
    $("stTimeframe").value = $("timeframe").value || "daily";
    renderRules(emptyRules());
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
        `<td>${esc(t.exit_date)}</td>` +
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
      if (row && e.target.classList.contains("cond-type")) syncCondRow(row);
    });
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

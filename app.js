(() => {
  const WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public";
  const SIGNAL_URL = "latest_signals.json";
  const markets = {
    XAUUSD: { symbol: "frxXAUUSD", label: "XAU/USD · Gold", decimals: 2 },
    EURUSD: { symbol: "frxEURUSD", label: "EUR/USD", decimals: 5 },
    GBPUSD: { symbol: "frxGBPUSD", label: "GBP/USD", decimals: 5 },
    USDJPY: { symbol: "frxUSDJPY", label: "USD/JPY", decimals: 3 }
  };

  const chartElement = document.getElementById("chart");
  const symbolSelect = document.getElementById("symbolSelect");
  const livePrice = document.getElementById("livePrice");
  const priceMeta = document.getElementById("priceMeta");
  const lastUpdated = document.getElementById("lastUpdated");
  const loader = document.getElementById("chartLoader");
  const connectionStatus = document.getElementById("connectionStatus");
  const timeframeButtons = Array.from(document.querySelectorAll("[data-granularity]"));
  const signalCard = document.getElementById("signalCard");
  const signalEyebrow = document.getElementById("signalEyebrow");
  const signalTitle = document.getElementById("signalTitle");
  const signalSummary = document.getElementById("signalSummary");
  const signalLevels = document.getElementById("signalLevels");

  let activeMarketKey = "XAUUSD";
  let granularity = 900;
  let socket = null;
  let candles = [];
  let currentCandle = null;
  let reconnectTimer = null;
  let intentionalClose = false;
  let signalPriceLines = [];
  let signalPollTimer = null;

  const chart = LightweightCharts.createChart(chartElement, {
    width: chartElement.clientWidth,
    height: chartElement.clientHeight,
    layout: {
      background: { color: "#06101d" },
      textColor: "#94a3b8"
    },
    grid: {
      vertLines: { color: "rgba(148, 163, 184, 0.06)" },
      horzLines: { color: "rgba(148, 163, 184, 0.06)" }
    },
    rightPriceScale: {
      borderColor: "rgba(148, 163, 184, 0.12)",
      scaleMargins: { top: 0.12, bottom: 0.12 }
    },
    timeScale: {
      borderColor: "rgba(148, 163, 184, 0.12)",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      barSpacing: 8
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: {
      timeFormatter: (timestamp) => {
        const date = new Date(Number(timestamp) * 1000);
        return new Intl.DateTimeFormat("en-GB", {
          timeZone: "Africa/Lagos",
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          hour12: true
        }).format(date);
      }
    }
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#20c67a",
    downColor: "#f05d6f",
    wickUpColor: "#20c67a",
    wickDownColor: "#f05d6f",
    borderVisible: false,
    priceLineVisible: true,
    lastValueVisible: true
  });

  const resizeObserver = new ResizeObserver(() => {
    chart.applyOptions({ width: chartElement.clientWidth, height: chartElement.clientHeight });
  });
  resizeObserver.observe(chartElement);

  function setStatus(state, text) {
    connectionStatus.dataset.state = state;
    connectionStatus.querySelector("span:last-child").textContent = text;
  }

  function setLoading(isLoading, text = "Loading candles…") {
    loader.textContent = text;
    loader.classList.toggle("hidden", !isLoading);
  }

  function formatPrice(value) {
    const market = markets[activeMarketKey];
    return Number(value).toFixed(market.decimals);
  }

  function updatePrice(value, epoch) {
    livePrice.textContent = formatPrice(value);
    const date = new Date(Number(epoch) * 1000);
    const time = new Intl.DateTimeFormat("en-NG", {
      timeZone: "Africa/Lagos",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      hour12: true
    }).format(date);
    priceMeta.textContent = `${markets[activeMarketKey].label} · WAT`;
    lastUpdated.textContent = `Last update: ${time}`;
  }

  function bucketStart(epoch) {
    return Math.floor(Number(epoch) / granularity) * granularity;
  }

  function normalizeCandle(item) {
    return {
      time: Number(item.epoch),
      open: Number(item.open),
      high: Number(item.high),
      low: Number(item.low),
      close: Number(item.close)
    };
  }

  function setSeriesPrecision() {
    const decimals = markets[activeMarketKey].decimals;
    candleSeries.applyOptions({
      priceFormat: {
        type: "price",
        precision: decimals,
        minMove: Math.pow(10, -decimals)
      }
    });
  }

  function handleHistory(message) {
    const raw = Array.isArray(message.candles) ? message.candles : [];
    candles = raw.map(normalizeCandle).sort((a, b) => a.time - b.time);
    candleSeries.setData(candles);
    currentCandle = candles.length ? { ...candles[candles.length - 1] } : null;
    if (currentCandle) updatePrice(currentCandle.close, currentCandle.time);
    chart.timeScale().fitContent();
    setLoading(false);
  }

  function handleTick(tick) {
    const epoch = Number(tick.epoch);
    const quote = Number(tick.quote);
    if (!Number.isFinite(epoch) || !Number.isFinite(quote)) return;

    const start = bucketStart(epoch);
    if (!currentCandle || start > currentCandle.time) {
      currentCandle = { time: start, open: quote, high: quote, low: quote, close: quote };
    } else if (start === currentCandle.time) {
      currentCandle.high = Math.max(currentCandle.high, quote);
      currentCandle.low = Math.min(currentCandle.low, quote);
      currentCandle.close = quote;
    } else {
      return;
    }

    candleSeries.update(currentCandle);
    updatePrice(quote, epoch);
  }

  function requestData(ws) {
    const market = markets[activeMarketKey];
    ws.send(JSON.stringify({
      ticks_history: market.symbol,
      adjust_start_time: 1,
      count: 500,
      end: "latest",
      start: 1,
      style: "candles",
      granularity
    }));
    ws.send(JSON.stringify({ ticks: market.symbol, subscribe: 1 }));
  }

  function connect() {
    clearTimeout(reconnectTimer);
    intentionalClose = false;
    setStatus("connecting", "Connecting");
    setLoading(true);

    const ws = new WebSocket(WS_URL);
    socket = ws;

    ws.addEventListener("open", () => {
      if (ws !== socket) return;
      setStatus("live", "Live");
      requestData(ws);
    });

    ws.addEventListener("message", (event) => {
      if (ws !== socket) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }

      if (message.error) {
        console.error("Deriv error:", message.error);
        setStatus("error", "Data error");
        setLoading(true, message.error.message || "Market data unavailable");
        return;
      }

      if (message.msg_type === "candles" || Array.isArray(message.candles)) handleHistory(message);
      if (message.msg_type === "tick" && message.tick) handleTick(message.tick);
    });

    ws.addEventListener("error", () => {
      if (ws !== socket) return;
      setStatus("error", "Connection error");
    });

    ws.addEventListener("close", () => {
      if (ws !== socket || intentionalClose) return;
      setStatus("connecting", "Reconnecting");
      reconnectTimer = setTimeout(connect, 2500);
    });
  }

  function clearSignalLines() {
    signalPriceLines.forEach((line) => {
      try { candleSeries.removePriceLine(line); } catch (_) {}
    });
    signalPriceLines = [];
  }

  function setNoSignal(message = "Waiting for a qualified BUY or SELL setup for the selected market.") {
    clearSignalLines();
    signalCard.dataset.signal = "WAIT";
    signalEyebrow.textContent = "V4 signal overlay";
    signalTitle.textContent = "No active signal";
    signalSummary.textContent = message;
    signalLevels.innerHTML = "<span>Entry —</span><span>SL —</span><span>TP1 —</span><span>TP2 —</span><span>TP3 —</span>";
  }

  function addPriceLine(price, title, color, style = LightweightCharts.LineStyle.Dashed) {
    if (!Number.isFinite(Number(price))) return;
    const line = candleSeries.createPriceLine({
      price: Number(price),
      color,
      lineWidth: 2,
      lineStyle: style,
      axisLabelVisible: true,
      title
    });
    signalPriceLines.push(line);
  }

  function renderSignal(signal) {
    clearSignalLines();
    const direction = String(signal.direction || "").toUpperCase();
    const expiry = signal.expires_at ? new Date(signal.expires_at) : null;
    if (!['BUY', 'SELL'].includes(direction) || !expiry || expiry <= new Date()) {
      setNoSignal("The previous V4 setup has expired. Waiting for the next qualified signal.");
      return;
    }

    addPriceLine(signal.entry_low, "ENTRY LOW", "#f4c152");
    addPriceLine(signal.entry_high, "ENTRY HIGH", "#f4c152");
    addPriceLine(signal.stop, "SL", "#ff5d73", LightweightCharts.LineStyle.Solid);
    addPriceLine(signal.tp1, "TP1", "#28d17c");
    addPriceLine(signal.tp2, "TP2", "#20b8ff");
    addPriceLine(signal.tp3, "TP3", "#9b8cff");

    const expiryText = new Intl.DateTimeFormat("en-NG", {
      timeZone: "Africa/Lagos",
      hour: "numeric",
      minute: "2-digit",
      hour12: true
    }).format(expiry);

    signalCard.dataset.signal = direction;
    signalEyebrow.textContent = `${direction} signal active · V4`;
    signalTitle.textContent = `${markets[activeMarketKey].label} ${direction}`;
    signalSummary.textContent = `Entry ${formatPrice(signal.entry_low)}–${formatPrice(signal.entry_high)} · expires ${expiryText} WAT. ${signal.reason || "Qualified V4 setup."}`;
    signalLevels.innerHTML = [
      `<span>Entry ${formatPrice(signal.entry_low)}–${formatPrice(signal.entry_high)}</span>`,
      `<span>SL ${formatPrice(signal.stop)}</span>`,
      `<span>TP1 ${formatPrice(signal.tp1)}</span>`,
      `<span>TP2 ${formatPrice(signal.tp2)}</span>`,
      `<span>TP3 ${formatPrice(signal.tp3)}</span>`
    ].join("");
  }

  async function loadSignalOverlay() {
    try {
      const response = await fetch(`${SIGNAL_URL}?v=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Signal HTTP ${response.status}`);
      const payload = await response.json();
      const signal = payload && payload.signals ? payload.signals[activeMarketKey] : null;
      if (!signal) {
        setNoSignal();
        return;
      }
      renderSignal(signal);
    } catch (error) {
      console.warn("Signal overlay unavailable:", error);
      setNoSignal("Signal overlay is temporarily unavailable; live candles remain connected.");
    }
  }

  function reloadMarket() {
    intentionalClose = true;
    clearTimeout(reconnectTimer);

    const oldSocket = socket;
    socket = null;
    if (oldSocket && oldSocket.readyState <= WebSocket.OPEN) oldSocket.close();

    candles = [];
    currentCandle = null;
    candleSeries.setData([]);
    livePrice.textContent = "—";
    priceMeta.textContent = "Waiting for market data";
    clearSignalLines();
    setSeriesPrecision();
    connect();
    loadSignalOverlay();
  }

  symbolSelect.addEventListener("change", (event) => {
    activeMarketKey = event.target.value;
    reloadMarket();
  });

  timeframeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      timeframeButtons.forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      granularity = Number(button.dataset.granularity);
      reloadMarket();
    });
  });

  window.addEventListener("beforeunload", () => {
    intentionalClose = true;
    clearTimeout(reconnectTimer);
    clearInterval(signalPollTimer);
    const ws = socket;
    socket = null;
    if (ws) ws.close();
  });

  setSeriesPrecision();
  connect();
  loadSignalOverlay();
  signalPollTimer = setInterval(loadSignalOverlay, 30000);
})();

/* global LightweightCharts */

const state = {
  payload: null,
  timeframe: "1min",
  strategy: "entry_point_2",
  chart: null,
  candleSeries: null,
  equityChart: null,
  initialCapital: 10000,
  selectedTradeId: null,
  selectedSignalId: null,
};

const byId = (id) => document.getElementById(id);
const isoInputValue = (value) => value ? value.slice(0, 16) : "";
const fmtNumber = (value, digits = 2) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
const fmtTime = (time) => new Date(time * 1000).toLocaleString("zh-CN", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const strategyLabel = (strategy) => ({ all: "\u5168\u90E8\u4FE1\u53F7", entry_point_2: "\u5165\u573A\u70B92", entry_point_3: "\u5165\u573A\u70B93" }[strategy] || strategy);
const timeframeLabel = (timeframe) => ({ "1min": "1\u5206\u949F", "5min": "5\u5206\u949F", "30min": "30\u5206\u949F" }[timeframe] || timeframe);
const sideLabel = (side) => ({ long: "\u505A\u591A", short: "\u505A\u7A7A" }[side] || side);
const trendLabel = (trend) => ({ up: "\u4E0A\u6DA8", down: "\u4E0B\u8DCC", unknown: "\u672A\u77E5" }[trend] || trend);
const exitReasonLabel = (reason) => ({ target: "\u6B62\u76C8", stop: "\u6B62\u635F", timeout: "\u8D85\u65F6", data_end: "\u6570\u636E\u7ED3\u675F" }[reason] || reason);
const timeframeSeconds = (timeframe) => ({ "1min": 60, "5min": 300, "30min": 1800 }[timeframe]);

function signalReasonLabel(reason) {
  const text = String(reason || "");
  const freshBreakout = text.match(/^fresh_close_breakout_(above|below)_(\d+)_bar_(high|low)$/);
  if (freshBreakout) {
    const [, direction, lookback] = freshBreakout;
    return direction === "above"
      ? `\u6536\u76D8\u4EF7\u5411\u4E0A\u7A81\u7834\u524D${lookback}\u6839K\u7EBF\u9AD8\u70B9`
      : `\u6536\u76D8\u4EF7\u5411\u4E0B\u8DCC\u7834\u524D${lookback}\u6839K\u7EBF\u4F4E\u70B9`;
  }
  return ({
    pullback_rebreakout_above_pullback_high: "\u56DE\u8C03\u540E\u5411\u4E0A\u7A81\u7834\u56DE\u8C03\u9AD8\u70B9",
    pullback_rebreakout_below_pullback_low: "\u56DE\u8C03\u540E\u5411\u4E0B\u8DCC\u7834\u56DE\u8C03\u4F4E\u70B9",
  }[text] || text.replaceAll("_", " "));
}

function qualityMessage(issue) {
  return issue.message;
}

function selectedStrategies() {
  return state.strategy === "all" ? ["entry_point_2", "entry_point_3"] : [state.strategy];
}

function selectedData() {
  return selectedStrategies().map((strategy) => state.payload.strategies[strategy]);
}

function selectedTrades() {
  return selectedData().flatMap(({ trades }) => trades["1min"]).sort((left, right) => left.entry_time - right.entry_time);
}

function selectedSignals() {
  return selectedData().flatMap(({ signals }) => signals["1min"]).sort((left, right) => left.signal_time - right.signal_time);
}

function findSignal(id) {
  return selectedSignals().find((signal) => signal.id === id) || null;
}

function findTrade(id) {
  return selectedTrades().find((trade) => trade.id === id) || null;
}

function signalForTrade(trade) {
  return selectedSignals().find((signal) => signal.strategy_id === trade.strategy_id && signal.signal_time === trade.signal_time && signal.side === trade.side) || null;
}

function activeSelection() {
  const trade = state.selectedTradeId ? findTrade(state.selectedTradeId) : null;
  const signal = trade ? signalForTrade(trade) : state.selectedSignalId ? findSignal(state.selectedSignalId) : null;
  return { signal, trade };
}

function selectTrade(trade) {
  state.selectedTradeId = trade ? trade.id : null;
  state.selectedSignalId = trade ? (signalForTrade(trade)?.id || null) : null;
  renderAll();
}

function selectSignal(signal) {
  state.selectedTradeId = null;
  state.selectedSignalId = signal ? signal.id : null;
  renderAll();
}

function clearSelection() {
  state.selectedTradeId = null;
  state.selectedSignalId = null;
}

function selectFirstInspectable() {
  const firstTrade = selectedTrades()[0];
  if (firstTrade) {
    state.selectedTradeId = firstTrade.id;
    state.selectedSignalId = signalForTrade(firstTrade)?.id || null;
    return;
  }
  const firstSignal = selectedSignals()[0];
  state.selectedTradeId = null;
  state.selectedSignalId = firstSignal?.id || null;
}

function aggregateMetrics() {
  const result = { signal_count: 0, trade_count: 0, net_pnl: 0, max_drawdown: 0, win_count: 0, loss_count: 0 };
  selectedData().forEach(({ metrics }) => {
    Object.keys(result).forEach((key) => { result[key] += Number(metrics[key] || 0); });
  });
  return result;
}

function selectedAnalysis() {
  return state.strategy === "all" ? null : state.payload.strategies[state.strategy].analysis;
}

function fmtRatio(value, digits = 2) {
  return value === null || value === undefined ? "-" : fmtNumber(value, digits);
}

function fmtPercent(value) {
  return value === null || value === undefined ? "-" : `${fmtNumber(value * 100, 1)}%`;
}

function fmtPnl(value) {
  return value === null || value === undefined ? "-" : `${value >= 0 ? "+" : ""}${fmtNumber(value)}`;
}

function initialCapital() {
  const input = byId("initial-capital");
  const value = Number(input?.value);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function capitalReturn(value) {
  const capital = initialCapital();
  return capital === null || value === null || value === undefined ? null : value / capital;
}

function positionSummary() {
  const position = state.payload.metadata.position || {};
  const margin = position.margin_per_trade === null ? "固定手数" : `$${fmtNumber(position.margin_per_trade)} 每笔`;
  return `${margin}，${fmtNumber(position.leverage, 0)}x 杠杆`;
}

function pnlClass(value) {
  return value > 0 ? "positive" : value < 0 ? "negative" : "";
}

function chartSignals() {
  return selectedData().flatMap(({ signals }) => signals[state.timeframe]);
}

function chartTrades() {
  return selectedData().flatMap(({ trades }) => trades[state.timeframe]);
}

function markers() {
  const marks = [];
  chartSignals().forEach((signal) => marks.push({
    id: `signal:${signal.id}`,
    time: signal.chart_time,
    position: signal.side === "long" ? "belowBar" : "aboveBar",
    color: signal.side === "long" ? "#0f766e" : "#c2413b",
    shape: signal.side === "long" ? "arrowUp" : "arrowDown",
    text: signal.strategy_id === "entry_point_2" ? "E2" : "E3",
  }));
  chartTrades().forEach((trade) => {
    marks.push({ id: `trade-entry:${trade.id}`, time: trade.chart_entry_time, position: trade.side === "long" ? "belowBar" : "aboveBar", color: "#17222b", shape: "circle", text: "IN" });
    marks.push({ id: `trade-exit:${trade.id}`, time: trade.chart_exit_time, position: trade.side === "long" ? "aboveBar" : "belowBar", color: trade.net_pnl >= 0 ? "#0f766e" : "#c2413b", shape: "square", text: "OUT" });
  });
  return marks.sort((left, right) => left.time - right.time);
}

function addSelectedPriceLines() {
  const { signal, trade } = activeSelection();
  if (!signal || !state.candleSeries) return;
  state.candleSeries.createPriceLine({ price: signal.breakout_level, color: "#c58b1b", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "\u7A81\u7834\u4F4D" });
  if (!trade) return;
  state.candleSeries.createPriceLine({ price: trade.entry_price, color: "#17222b", lineWidth: 1, lineStyle: 0, axisLabelVisible: false, title: "\u5165\u573A" });
  state.candleSeries.createPriceLine({ price: trade.target_price, color: "#0f766e", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "\u6B62\u76C8" });
  state.candleSeries.createPriceLine({ price: trade.stop_price, color: "#c2413b", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "\u6B62\u635F" });
}

function focusSelection() {
  const { signal, trade } = activeSelection();
  const center = trade?.entry_time || signal?.signal_time;
  if (!center || !state.chart) return;
  const unit = timeframeSeconds(state.timeframe);
  state.chart.timeScale().setVisibleRange({ from: center - unit * 25, to: (trade?.exit_time || center) + unit * 35 });
}

function renderChart() {
  const root = byId("chart");
  root.replaceChildren();
  if (!window.LightweightCharts) throw new Error("Lightweight Charts \u52A0\u8F7D\u5931\u8D25\uFF0C\u8BF7\u68C0\u67E5\u7F51\u7EDC\u8FDE\u63A5\u540E\u5237\u65B0\u9875\u9762\u3002");
  const candles = state.payload.series[state.timeframe] || [];
  const chartData = candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close }));
  state.chart = LightweightCharts.createChart(root, {
    width: root.clientWidth,
    height: root.clientHeight,
    layout: { background: { type: "solid", color: "#ffffff" }, textColor: "#65727c", fontFamily: '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", Inter, "Segoe UI", Arial, sans-serif' },
    grid: { vertLines: { color: "#f0f3f2" }, horzLines: { color: "#f0f3f2" } },
    rightPriceScale: { borderColor: "#d9e0e3" },
    timeScale: { borderColor: "#d9e0e3", timeVisible: true, secondsVisible: false },
    crosshair: { vertLine: { labelBackgroundColor: "#17222b" }, horzLine: { labelBackgroundColor: "#17222b" } },
  });
  state.candleSeries = state.chart.addCandlestickSeries({ upColor: "#0f766e", downColor: "#c2413b", borderUpColor: "#0f766e", borderDownColor: "#c2413b", wickUpColor: "#0f766e", wickDownColor: "#c2413b" });
  const fastEma = state.chart.addLineSeries({ color: "#2563eb", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  const slowEma = state.chart.addLineSeries({ color: "#c58b1b", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  state.candleSeries.setData(chartData);
  fastEma.setData(candles.filter((candle) => candle.ema_fast !== null).map((candle) => ({ time: candle.time, value: candle.ema_fast })));
  slowEma.setData(candles.filter((candle) => candle.ema_slow !== null).map((candle) => ({ time: candle.time, value: candle.ema_slow })));
  state.candleSeries.setMarkers(markers());
  addSelectedPriceLines();
  state.chart.timeScale().fitContent();
  focusSelection();
  byId("bar-count").textContent = `${candles.length.toLocaleString("zh-CN")} \u6839${timeframeLabel(state.timeframe)}K\u7EBF`;
  const { ema_fast: fast, ema_slow: slow } = state.payload.metadata.trend;
  byId("chart-title").textContent = `${timeframeLabel(state.timeframe)}K\u7EBF \u00B7 EMA ${fast}/${slow}`;

  const tooltip = byId("chart-tooltip");
  const byTime = new Map(candles.map((candle) => [candle.time, candle]));
  state.chart.subscribeCrosshairMove((param) => {
    const candle = byTime.get(param.time);
    if (!candle) { tooltip.hidden = true; return; }
    tooltip.hidden = false;
    tooltip.innerHTML = `<strong>${fmtTime(candle.time)}</strong><br>\u5F00 ${fmtNumber(candle.open)} &nbsp; \u9AD8 ${fmtNumber(candle.high)}<br>\u4F4E ${fmtNumber(candle.low)} &nbsp; \u6536 ${fmtNumber(candle.close)}<br>EMA ${fast}: ${candle.ema_fast === null ? "-" : fmtNumber(candle.ema_fast)} &nbsp; EMA ${slow}: ${candle.ema_slow === null ? "-" : fmtNumber(candle.ema_slow)}<br>\u8D8B\u52BF\uFF1A${trendLabel(candle.trend)}`;
  });
  state.chart.subscribeClick((param) => {
    const markerId = param.hoveredMarkerId;
    if (!markerId) return;
    if (markerId.startsWith("signal:")) selectSignal(findSignal(markerId.slice(7)));
    if (markerId.startsWith("trade-entry:") || markerId.startsWith("trade-exit:")) selectTrade(findTrade(markerId.slice(markerId.indexOf(":", 6) + 1)));
  });
  new ResizeObserver(([entry]) => state.chart.applyOptions({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) })).observe(root);
}

function renderSummary() {
  const metrics = aggregateMetrics();
  const total = metrics.win_count + metrics.loss_count;
  byId("strategy-title").textContent = strategyLabel(state.strategy);
  byId("metric-signals").textContent = metrics.signal_count;
  if (state.strategy === "all") {
    byId("metric-trades").textContent = "-";
    byId("metric-pnl").textContent = "-";
    byId("metric-pnl").className = "";
    byId("metric-dd").textContent = "-";
    byId("metric-win-rate").textContent = "-";
    return;
  }
  byId("metric-trades").textContent = metrics.trade_count;
  const pnl = byId("metric-pnl");
  pnl.textContent = `${metrics.net_pnl >= 0 ? "+" : ""}${fmtNumber(metrics.net_pnl)}`;
  pnl.className = metrics.net_pnl >= 0 ? "positive" : "negative";
  byId("metric-dd").textContent = fmtNumber(metrics.max_drawdown);
  byId("metric-win-rate").textContent = total ? `${Math.round((metrics.win_count / total) * 100)}%` : "-";
}

function setAnalysisValue(id, value, className = "") {
  const element = byId(id);
  element.textContent = value;
  element.className = className;
}

function renderEquityChart(points) {
  const root = byId("equity-chart");
  root.replaceChildren();
  if (!points.length || !window.LightweightCharts) {
    root.textContent = "当前区间没有可绘制的已完成交易。";
    return;
  }
  state.equityChart = LightweightCharts.createChart(root, {
    width: root.clientWidth,
    height: root.clientHeight,
    layout: { background: { type: "solid", color: "#ffffff" }, textColor: "#65727c", fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif' },
    grid: { vertLines: { color: "#f0f3f2" }, horzLines: { color: "#f0f3f2" } },
    rightPriceScale: { borderColor: "#d9e0e3" },
    timeScale: { borderColor: "#d9e0e3", timeVisible: true, secondsVisible: false },
  });
  const series = state.equityChart.addAreaSeries({ lineColor: "#0f766e", topColor: "rgba(15, 118, 110, 0.18)", bottomColor: "rgba(15, 118, 110, 0.02)", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });
  series.setData(points);
  state.equityChart.timeScale().fitContent();
  new ResizeObserver(([entry]) => state.equityChart.applyOptions({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) })).observe(root);
}

function renderAttribution(id, rows, label, valueKey) {
  const root = byId(id);
  root.replaceChildren();
  if (!rows.length) {
    root.textContent = "当前区间没有已完成交易。";
    return;
  }
  rows.forEach((row) => {
    const item = document.createElement("div");
    const value = row[valueKey];
    item.className = "attribution-row";
    item.innerHTML = `<span>${label(row)}</span><strong class="${pnlClass(value)}">${fmtPnl(value)}</strong><em>${row.trade_count} 笔</em>`;
    root.append(item);
  });
}

function renderComparison() {
  const body = byId("comparison-body");
  body.replaceChildren();
  const second = state.payload.strategies.entry_point_2.analysis;
  const third = state.payload.strategies.entry_point_3.analysis;
  const rows = [
    ["信号数", second.signal_count, third.signal_count, (value) => value],
    ["完成交易", second.trade_count, third.trade_count, (value) => value],
    ["成交率", second.fill_rate, third.fill_rate, fmtPercent],
    ["净盈亏", second.net_pnl, third.net_pnl, fmtPnl],
    ["账户收益率", capitalReturn(second.net_pnl), capitalReturn(third.net_pnl), fmtPercent],
    ["胜率", second.win_rate, third.win_rate, fmtPercent],
    ["利润因子", second.profit_factor, third.profit_factor, fmtRatio],
    ["最大回撤", state.payload.strategies.entry_point_2.metrics.max_drawdown, state.payload.strategies.entry_point_3.metrics.max_drawdown, (value) => fmtNumber(value)],
    ["平均单笔", second.average_pnl, third.average_pnl, fmtPnl],
    ["连续亏损", second.max_consecutive_losses, third.max_consecutive_losses, (value) => value],
  ];
  rows.forEach(([label, secondValue, thirdValue, format]) => {
    const row = document.createElement("tr");
    row.innerHTML = `<th>${label}</th><td class="${typeof secondValue === "number" ? pnlClass(label.includes("盈亏") || label.includes("单笔") ? secondValue : 0) : ""}">${format(secondValue)}</td><td class="${typeof thirdValue === "number" ? pnlClass(label.includes("盈亏") || label.includes("单笔") ? thirdValue : 0) : ""}">${format(thirdValue)}</td>`;
    body.append(row);
  });
}

function renderAnalysis() {
  const isComparison = state.strategy === "all";
  byId("comparison-view").hidden = !isComparison;
  byId("single-analysis-view").hidden = isComparison;
  byId("analysis-title").textContent = isComparison ? "策略对比" : `${strategyLabel(state.strategy)} 回测分析`;
  byId("analysis-subtitle").textContent = isComparison ? "两套策略独立回测，未合并为组合权益曲线。" : positionSummary();
  if (isComparison) {
    renderComparison();
    return;
  }
  const analysis = selectedAnalysis();
  const maxDrawdown = state.payload.strategies[state.strategy].metrics.max_drawdown;
  const capital = initialCapital();
  setAnalysisValue("analysis-final-equity", fmtPnl(analysis.net_pnl), pnlClass(analysis.net_pnl));
  setAnalysisValue("analysis-initial-capital", capital === null ? "请填写有效资金" : `$${fmtNumber(capital)}`);
  setAnalysisValue("analysis-return-rate", fmtPercent(capitalReturn(analysis.net_pnl)), pnlClass(analysis.net_pnl));
  setAnalysisValue("analysis-max-drawdown", fmtPnl(-maxDrawdown), "negative");
  setAnalysisValue("analysis-drawdown-rate", fmtPercent(capitalReturn(-maxDrawdown)), "negative");
  setAnalysisValue("analysis-profit-factor", fmtRatio(analysis.profit_factor));
  setAnalysisValue("analysis-payoff", fmtRatio(analysis.payoff_ratio));
  setAnalysisValue("analysis-average-pnl", fmtPnl(analysis.average_pnl), pnlClass(analysis.average_pnl));
  setAnalysisValue("analysis-average-hold", analysis.average_hold_bars === null ? "-" : `${fmtNumber(analysis.average_hold_bars, 1)} 根K线`);
  setAnalysisValue("analysis-largest-win", fmtPnl(analysis.largest_win), pnlClass(analysis.largest_win));
  setAnalysisValue("analysis-largest-loss", fmtPnl(analysis.largest_loss), pnlClass(analysis.largest_loss));
  setAnalysisValue("analysis-fill-rate", fmtPercent(analysis.fill_rate));
  setAnalysisValue("analysis-unfilled", `${analysis.unfilled_signal_count} / ${analysis.signal_count}`);
  setAnalysisValue("analysis-win-streak", `${analysis.max_consecutive_wins} 笔`);
  setAnalysisValue("analysis-loss-streak", `${analysis.max_consecutive_losses} 笔`);
  setAnalysisValue("analysis-long-pnl", fmtPnl(analysis.by_side.long.net_pnl), pnlClass(analysis.by_side.long.net_pnl));
  setAnalysisValue("analysis-short-pnl", fmtPnl(analysis.by_side.short.net_pnl), pnlClass(analysis.by_side.short.net_pnl));
  renderEquityChart(analysis.equity_curve);
  renderAttribution("exit-analysis", Object.entries(analysis.by_exit_reason).map(([reason, value]) => ({ ...value, reason })), (row) => exitReasonLabel(row.reason), "net_pnl");
  renderAttribution("daily-analysis", analysis.daily_pnl, (row) => row.date, "net_pnl");
}

function setTrend(id, value) {
  const element = byId(id);
  element.textContent = trendLabel(value);
  element.className = String(value).toLowerCase();
}

function renderReview() {
  const { signal, trade } = activeSelection();
  byId("review-empty").hidden = Boolean(signal);
  byId("review-content").hidden = !signal;
  const trades = selectedTrades();
  const selectedIndex = trade ? trades.findIndex((item) => item.id === trade.id) : -1;
  byId("previous-trade").disabled = selectedIndex <= 0;
  byId("next-trade").disabled = selectedIndex < 0 || selectedIndex >= trades.length - 1;
  if (!signal) return;
  byId("review-strategy").textContent = strategyLabel(signal.strategy_id);
  const side = byId("review-side");
  side.textContent = sideLabel(signal.side);
  side.className = `side ${signal.side}`;
  byId("review-reason").textContent = signalReasonLabel(signal.reason);
  byId("review-signal-time").textContent = fmtTime(signal.signal_time);
  byId("review-entry-time").textContent = signal.entry_time ? fmtTime(signal.entry_time) : "\u65E0\u4E0B\u4E00\u6839K\u7EBF";
  byId("review-breakout").textContent = signal.breakout_level === null ? "-" : fmtNumber(signal.breakout_level);
  byId("review-atr").textContent = signal.atr === null ? "-" : fmtNumber(signal.atr, 3);
  setTrend("trend-base", signal.base_trend);
  setTrend("trend-medium", signal.medium_trend);
  setTrend("trend-large", signal.large_trend);
  byId("trade-execution").hidden = !trade;
  if (trade) {
    byId("review-entry-price").textContent = fmtNumber(trade.entry_price);
    byId("review-stop").textContent = fmtNumber(trade.stop_price);
    byId("review-target").textContent = fmtNumber(trade.target_price);
    byId("review-exit").textContent = `${fmtNumber(trade.exit_price)} \u00B7 ${exitReasonLabel(trade.exit_reason)}`;
    const pnl = byId("review-pnl");
    pnl.textContent = `${trade.net_pnl >= 0 ? "+" : ""}${fmtNumber(trade.net_pnl)}`;
    pnl.className = trade.net_pnl >= 0 ? "positive" : "negative";
    byId("review-lots").textContent = `${fmtNumber(trade.lots, 2)} 手 (${fmtNumber(trade.quantity, 2)} 盎)`;
    byId("review-notional").textContent = `$${fmtNumber(trade.notional_value)}`;
    byId("review-margin").textContent = `$${fmtNumber(trade.required_margin)}`;
    byId("review-hold").textContent = `${trade.hold_bars} \u6839K\u7EBF`;
    byId("review-note").textContent = "\u56FE\u8868\u4EC5\u7A81\u51FA\u663E\u793A\u5F53\u524D\u4EA4\u6613\u7684\u7A81\u7834\u4F4D\u3001\u5165\u573A\u4F4D\u3001\u6B62\u635F\u4F4D\u548C\u6B62\u76C8\u4F4D\u3002";
  } else {
    byId("review-note").textContent = "\u8BE5\u4FE1\u53F7\u5DF2\u8BB0\u5F55\uFF0C\u4F46\u5728\u5F53\u524D\u533A\u95F4\u5185\u672A\u5F62\u6210\u5DF2\u5B8C\u6210\u4EA4\u6613\u3002";
  }
}

function renderLedger() {
  const trades = selectedTrades();
  const body = byId("trades-body");
  body.replaceChildren();
  trades.forEach((trade) => {
    const row = document.createElement("tr");
    const strategy = strategyLabel(trade.strategy_id);
    row.tabIndex = 0;
    row.className = trade.id === state.selectedTradeId ? "selected" : "";
    row.innerHTML = `<td>${strategy}</td><td><span class="side ${trade.side}">${sideLabel(trade.side)}</span></td><td>${fmtTime(trade.entry_time)}</td><td>${fmtTime(trade.exit_time)}</td><td>${exitReasonLabel(trade.exit_reason)}</td><td class="${trade.net_pnl >= 0 ? "positive" : "negative"}">${trade.net_pnl >= 0 ? "+" : ""}${fmtNumber(trade.net_pnl)}</td><td>${trade.hold_bars} \u6839</td>`;
    row.addEventListener("click", () => selectTrade(trade));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectTrade(trade); } });
    body.append(row);
  });
  byId("no-trades").hidden = trades.length > 0;
}

function renderAll() {
  renderChart();
  renderSummary();
  renderAnalysis();
  renderReview();
  renderLedger();
}

function moveTrade(offset) {
  const trades = selectedTrades();
  const index = trades.findIndex((trade) => trade.id === state.selectedTradeId);
  const target = trades[index + offset];
  if (target) selectTrade(target);
}

function renderMetadata() {
  const { metadata, quality_issues: issues } = state.payload;
  byId("instrument-name").textContent = metadata.symbol;
  byId("window-label").textContent = `${metadata.display_start.slice(0, 16).replace("T", " ")} \u81F3 ${metadata.display_end.slice(0, 16).replace("T", " ")} UTC`;
  byId("source-label").textContent = `${metadata.provider.toUpperCase()} ${({ mid: "\u4E2D\u95F4\u4EF7", bid: "\u4E70\u5165\u4EF7", ask: "\u5356\u51FA\u4EF7" }[metadata.price_basis] || metadata.price_basis)}`;
  if (issues.length) {
    byId("warning-band").hidden = false;
    byId("warning-copy").textContent = issues.map(qualityMessage).join(" ");
  } else {
    byId("warning-band").hidden = true;
  }
}

function renderRangeInputs() {
  const { metadata } = state.payload;
  const start = byId("range-start");
  const end = byId("range-end");
  start.removeAttribute("min");
  start.removeAttribute("max");
  end.removeAttribute("min");
  end.removeAttribute("max");
  start.value = isoInputValue(metadata.display_start);
  end.value = isoInputValue(metadata.display_end);
}

function renderPositionInputs() {
  const position = state.payload.metadata.position || {};
  byId("margin-per-trade").value = position.margin_per_trade ?? 1000;
  byId("position-leverage").value = position.leverage ?? 20;
}

function setRangeMessage(message = "") {
  const element = byId("range-message");
  element.hidden = !message;
  element.textContent = message;
}

function activePositionParams() {
  return {
    margin_per_trade: byId("margin-per-trade")?.value,
    leverage: byId("position-leverage")?.value,
  };
}

async function loadPayload(start, end, position = activePositionParams()) {
  const search = new URLSearchParams();
  if (start && end) {
    search.set("start", `${start}:00Z`);
    search.set("end", `${end}:00Z`);
  }
  if (position.margin_per_trade) search.set("margin_per_trade", position.margin_per_trade);
  if (position.leverage) search.set("leverage", position.leverage);
  const params = search.size ? `?${search}` : "";
  const response = await fetch(`/api/dashboard${params}`, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `\u770B\u677F\u6570\u636E\u8BF7\u6C42\u5931\u8D25\uFF08HTTP ${response.status}\uFF09\u3002`);
  return body;
}

async function applyRange(event) {
  event.preventDefault();
  const start = byId("range-start").value;
  const end = byId("range-end").value;
  if (!start || !end) return;
  const button = byId("apply-range");
  button.disabled = true;
  setRangeMessage("");
  try {
    state.payload = await loadPayload(start, end);
    clearSelection();
    renderMetadata();
    renderRangeInputs();
    renderPositionInputs();
    selectFirstInspectable();
    renderAll();
  } catch (error) {
    setRangeMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

async function applyPosition(event) {
  event.preventDefault();
  const margin = Number(byId("margin-per-trade").value);
  const leverage = Number(byId("position-leverage").value);
  if (!Number.isFinite(margin) || margin <= 0 || !Number.isFinite(leverage) || leverage <= 0) {
    setRangeMessage("每次交易金额和杠杆必须为正数。");
    return;
  }
  const button = byId("apply-position");
  button.disabled = true;
  setRangeMessage("");
  try {
    state.payload = await loadPayload();
    clearSelection();
    renderMetadata();
    renderPositionInputs();
    selectFirstInspectable();
    renderAll();
  } catch (error) {
    setRangeMessage(error.message);
  } finally {
    button.disabled = false;
  }
}

async function start() {
  state.payload = await loadPayload(null, null, {});
  renderMetadata();
  renderRangeInputs();
  renderPositionInputs();
  selectFirstInspectable();
  byId("date-range-form").addEventListener("submit", applyRange);
  byId("position-form").addEventListener("submit", applyPosition);
  byId("initial-capital").addEventListener("input", () => renderAnalysis());
  document.querySelectorAll("input[name=timeframe]").forEach((input) => input.addEventListener("change", () => { state.timeframe = input.value; renderAll(); }));
  document.querySelectorAll("input[name=strategy]").forEach((input) => input.addEventListener("change", () => { state.strategy = input.value; selectFirstInspectable(); renderAll(); }));
  byId("previous-trade").addEventListener("click", () => moveTrade(-1));
  byId("next-trade").addEventListener("click", () => moveTrade(1));
  renderAll();
}

start().catch((error) => {
  document.body.innerHTML = `<main class="fatal"><h1>\u56FE\u8868\u4E0D\u53EF\u7528</h1><p>${error.message}</p></main>`;
});

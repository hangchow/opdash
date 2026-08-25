let initialized = false;
let refreshTimer = null;
let refreshMs = 1000;
const fullscreenState = {
  card: null,
  chartId: null,
  button: null,
  backdrop: null,
};
const LEGEND_SHORT_COLOR = "rgb(0, 153, 0)";
const LEGEND_LONG_COLOR = "rgb(255, 104, 181)";
const LEGEND_HOLLOW_COLOR = "#ffffff";
const Y_RANGE_PAD_RATIO = 0.1;
const Y_RANGE_EDGE_TRIGGER_RATIO = 0.1;
const Y_RANGE_MIN_PAD = 1.0;
const HOVER_LABEL_SIDE_MARGIN = 10;
const OPTION_TOOLTIP_OFFSET = 14;
const OPTION_TOOLTIP_HIDE_DELAY_MS = 450;
const panelYRangeState = new Map();

function formatQuantity(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) {
    return "N/A";
  }
  const rounded = Math.round(num);
  if (Math.abs(num - rounded) < 1e-9) {
    return String(rounded);
  }
  return num.toFixed(3).replace(/\.?0+$/, "");
}

function optionCoordinateKey(option) {
  return JSON.stringify([option.strike_date, Number(option.strike_price)]);
}

function optionTooltipOrder(option) {
  const sideOrder = option.side === "SHORT" ? 0 : 1;
  const typeOrder = option.type === "CALL" ? 0 : 1;
  return sideOrder * 2 + typeOrder;
}

function buildGroupedHoverTexts(options) {
  const groups = new Map();
  options.forEach((option) => {
    const key = optionCoordinateKey(option);
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(option);
  });
  groups.forEach((group) => {
    group.sort((left, right) => optionTooltipOrder(left) - optionTooltipOrder(right));
  });
  return options.map((option) =>
    groups.get(optionCoordinateKey(option)).map((item) => item.hover_text)
  );
}

function panelId(portIndex, stockCode) {
  return `panel-${portIndex}-${stockCode.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function parseTranslate(transformText) {
  const match = /translate\(([-\d.]+)(?:[,\s]+([-\d.]+))?\)/.exec(transformText || "");
  if (!match) {
    return null;
  }
  return {
    raw: match[0],
    x: Number(match[1]),
    y: Number(match[2] || 0),
  };
}

function clampHoverLabels(chartEl) {
  if (!chartEl) {
    return;
  }
  const chartRect = chartEl.getBoundingClientRect();
  if (!chartRect.width) {
    return;
  }

  chartEl.querySelectorAll(".hoverlayer .hovertext").forEach((label) => {
    const labelRect = label.getBoundingClientRect();
    if (!labelRect.width) {
      return;
    }

    const translate = parseTranslate(label.getAttribute("transform"));
    if (!translate) {
      return;
    }

    const minLeft = chartRect.left + HOVER_LABEL_SIDE_MARGIN;
    const maxLeft = Math.max(
      minLeft,
      chartRect.right - HOVER_LABEL_SIDE_MARGIN - labelRect.width
    );
    const nextLeft = clamp(labelRect.left, minLeft, maxLeft);
    const deltaX = nextLeft - labelRect.left;

    if (Math.abs(deltaX) < 0.5) {
      return;
    }

    label.setAttribute(
      "transform",
      (label.getAttribute("transform") || "").replace(
        translate.raw,
        `translate(${(translate.x + deltaX).toFixed(2)}, ${translate.y.toFixed(2)})`
      )
    );
  });
}

function scheduleHoverLabelClamp(chartEl) {
  if (!chartEl || chartEl.dataset.hoverClampPending === "true") {
    return;
  }
  chartEl.dataset.hoverClampPending = "true";
  requestAnimationFrame(() => {
    chartEl.dataset.hoverClampPending = "false";
    clampHoverLabels(chartEl);
  });
}

function bindHoverLabelClamp(chartEl) {
  if (!chartEl || chartEl.dataset.hoverClampBound === "true") {
    return;
  }
  chartEl.dataset.hoverClampBound = "true";

  const schedule = () => scheduleHoverLabelClamp(chartEl);
  if (typeof chartEl.on === "function") {
    chartEl.on("plotly_hover", schedule);
    chartEl.on("plotly_afterplot", schedule);
    chartEl.on("plotly_relayout", schedule);
    chartEl.on("plotly_redraw", schedule);
  }

  chartEl.addEventListener(
    "mousemove",
    () => {
      if (chartEl.querySelector(".hoverlayer .hovertext")) {
        schedule();
      }
    },
    { passive: true }
  );
  window.addEventListener("resize", schedule, { passive: true });
}

function clearOptionTooltipHide(tooltip) {
  const timerId = Number(tooltip.dataset.hideTimer || 0);
  if (timerId) {
    window.clearTimeout(timerId);
    tooltip.dataset.hideTimer = "";
  }
}

function hideOptionTooltip(tooltip) {
  clearOptionTooltipHide(tooltip);
  tooltip.style.visibility = "hidden";
  tooltip.hidden = true;
  tooltip.dataset.visible = "false";
}

function scheduleOptionTooltipHide(tooltip) {
  if (!tooltip || tooltip.dataset.visible !== "true") {
    return;
  }
  clearOptionTooltipHide(tooltip);
  tooltip.dataset.hideTimer = String(
    window.setTimeout(() => {
      if (tooltip.matches(":hover")) {
        return;
      }
      hideOptionTooltip(tooltip);
    }, OPTION_TOOLTIP_HIDE_DELAY_MS)
  );
}

function splitOptionTooltipLines(text) {
  return String(text || "").split(/<br\s*\/?>/i);
}

function appendOptionTooltipLines(container, lines) {
  lines.forEach((line, index) => {
    if (index > 0) {
      container.appendChild(document.createElement("br"));
    }
    container.appendChild(document.createTextNode(line));
  });
}

function setOptionTooltipContent(tooltip, texts) {
  tooltip.replaceChildren();
  const entries = (Array.isArray(texts) ? texts : [texts])
    .filter(Boolean)
    .map(splitOptionTooltipLines);
  const sharedHeader =
    entries.length > 1 && entries.every((lines) => lines[0] === entries[0][0]);

  if (sharedHeader) {
    const header = document.createElement("div");
    header.className = "option-tooltip-header";
    header.textContent = entries[0][0];
    tooltip.appendChild(header);
  }

  entries.forEach((lines) => {
    const entry = document.createElement("div");
    entry.className = "option-tooltip-entry";
    appendOptionTooltipLines(entry, sharedHeader ? lines.slice(1) : lines);
    tooltip.appendChild(entry);
  });
}

function positionOptionTooltip(chartEl, tooltip, clientX, clientY) {
  const card = chartEl.closest(".card") || chartEl.parentElement || chartEl;
  const chartRect = chartEl.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const tooltipRect = tooltip.getBoundingClientRect();
  const minLeft = chartRect.left - cardRect.left + HOVER_LABEL_SIDE_MARGIN;
  const maxLeft = Math.max(
    minLeft,
    chartRect.right - cardRect.left - tooltipRect.width - HOVER_LABEL_SIDE_MARGIN
  );
  const minTop = chartRect.top - cardRect.top + HOVER_LABEL_SIDE_MARGIN;
  const maxTop = Math.max(
    minTop,
    chartRect.bottom - cardRect.top - tooltipRect.height - HOVER_LABEL_SIDE_MARGIN
  );

  tooltip.style.left = `${clamp(
    clientX - cardRect.left + OPTION_TOOLTIP_OFFSET,
    minLeft,
    maxLeft
  )}px`;
  tooltip.style.top = `${clamp(
    clientY - cardRect.top + OPTION_TOOLTIP_OFFSET,
    minTop,
    maxTop
  )}px`;
}

function ensureOptionTooltip(chartEl) {
  const card = chartEl.closest(".card") || chartEl.parentElement || chartEl;
  let tooltip = card.querySelector(".option-hover-tooltip");
  if (tooltip) {
    return tooltip;
  }

  tooltip = document.createElement("div");
  tooltip.className = "option-hover-tooltip";
  tooltip.hidden = true;
  tooltip.style.visibility = "hidden";
  tooltip.dataset.visible = "false";
  tooltip.setAttribute("role", "tooltip");
  tooltip.addEventListener("mouseenter", () => clearOptionTooltipHide(tooltip));
  tooltip.addEventListener("mouseleave", () => scheduleOptionTooltipHide(tooltip));
  card.appendChild(tooltip);
  return tooltip;
}

function getOptionTooltipTexts(point) {
  if (!point) {
    return [];
  }
  const pointIndex = Number.isInteger(point.pointIndex)
    ? point.pointIndex
    : point.pointNumber;
  const groupedTexts =
    point.customdata ||
    (point.data && point.data.customdata && point.data.customdata[pointIndex]);
  if (Array.isArray(groupedTexts)) {
    return groupedTexts.filter(Boolean);
  }

  const pointText =
    point.text || (point.data && point.data.text && point.data.text[pointIndex]);
  return pointText ? [pointText] : [];
}

function showOptionTooltip(chartEl, eventData) {
  const point = eventData && eventData.points && eventData.points[0];
  const tooltipTexts = getOptionTooltipTexts(point);
  if (tooltipTexts.length === 0) {
    return;
  }
  const sourceEvent = eventData.event || {};
  const chartRect = chartEl.getBoundingClientRect();
  const clientX = Number.isFinite(sourceEvent.clientX)
    ? sourceEvent.clientX
    : chartRect.left + chartRect.width / 2;
  const clientY = Number.isFinite(sourceEvent.clientY)
    ? sourceEvent.clientY
    : chartRect.top + chartRect.height / 2;
  const tooltip = ensureOptionTooltip(chartEl);
  const wasVisible = tooltip.dataset.visible === "true";

  clearOptionTooltipHide(tooltip);
  setOptionTooltipContent(tooltip, tooltipTexts);
  if (!wasVisible) {
    tooltip.style.visibility = "hidden";
  }
  tooltip.hidden = false;
  tooltip.dataset.visible = "true";
  requestAnimationFrame(() => {
    if (tooltip.dataset.visible === "true") {
      positionOptionTooltip(chartEl, tooltip, clientX, clientY);
      tooltip.style.visibility = "visible";
    }
  });
}

function bindOptionTooltip(chartEl) {
  if (!chartEl || chartEl.dataset.optionTooltipBound === "true") {
    return;
  }
  chartEl.dataset.optionTooltipBound = "true";

  if (typeof chartEl.on === "function") {
    chartEl.on("plotly_hover", (eventData) => showOptionTooltip(chartEl, eventData));
    chartEl.on("plotly_unhover", () => scheduleOptionTooltipHide(ensureOptionTooltip(chartEl)));
    chartEl.on("plotly_relayout", () => hideOptionTooltip(ensureOptionTooltip(chartEl)));
  }

  chartEl.addEventListener(
    "mouseleave",
    () => scheduleOptionTooltipHide(ensureOptionTooltip(chartEl)),
    { passive: true }
  );
}

function maximizeButtonIcon() {
  return `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path
        d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  `;
}

function resizeChart(chartId) {
  const chartEl = document.getElementById(chartId);
  if (!chartEl || !window.Plotly || !window.Plotly.Plots) {
    return;
  }
  window.Plotly.Plots.resize(chartEl);
}

function exitFullscreenCard() {
  if (!fullscreenState.card) {
    return;
  }
  fullscreenState.card.classList.remove("card-fullscreen");
  if (fullscreenState.button) {
    fullscreenState.button.classList.remove("is-active");
    fullscreenState.button.title = "Maximize chart";
    fullscreenState.button.setAttribute("aria-label", "Maximize chart");
  }
  if (fullscreenState.backdrop && fullscreenState.backdrop.parentNode) {
    fullscreenState.backdrop.parentNode.removeChild(fullscreenState.backdrop);
  }
  document.body.classList.remove("chart-fullscreen-active");
  const chartId = fullscreenState.chartId;
  fullscreenState.card = null;
  fullscreenState.chartId = null;
  fullscreenState.button = null;
  fullscreenState.backdrop = null;
  requestAnimationFrame(() => resizeChart(chartId));
}

function enterFullscreenCard(card, chartId, button) {
  if (fullscreenState.card === card) {
    return;
  }
  exitFullscreenCard();
  const backdrop = document.createElement("div");
  backdrop.className = "chart-backdrop";
  backdrop.addEventListener("click", () => exitFullscreenCard());
  document.body.appendChild(backdrop);
  document.body.classList.add("chart-fullscreen-active");
  card.classList.add("card-fullscreen");
  button.classList.add("is-active");
  button.title = "Exit fullscreen";
  button.setAttribute("aria-label", "Exit fullscreen");
  fullscreenState.card = card;
  fullscreenState.chartId = chartId;
  fullscreenState.button = button;
  fullscreenState.backdrop = backdrop;
  requestAnimationFrame(() => resizeChart(chartId));
}

function toggleFullscreenCard(card, chartId, button) {
  if (fullscreenState.card === card) {
    exitFullscreenCard();
    return;
  }
  enterFullscreenCard(card, chartId, button);
}

function createFullscreenButton(card, chartId) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "card-fullscreen-btn";
  button.title = "Maximize chart";
  button.setAttribute("aria-label", "Maximize chart");
  button.innerHTML = maximizeButtonIcon();
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggleFullscreenCard(card, chartId, button);
  });
  return button;
}

function toFiniteNumber(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function computePanelYRange(strikePrices, stockPrice) {
  const candidates = [];
  (strikePrices || []).forEach((value) => {
    const num = toFiniteNumber(value);
    if (num !== null) {
      candidates.push(num);
    }
  });
  const priceNum = toFiniteNumber(stockPrice);
  if (priceNum !== null) {
    candidates.push(priceNum);
  }
  if (candidates.length === 0) {
    return null;
  }
  const yMin = Math.min(...candidates);
  const yMax = Math.max(...candidates);
  const yPad = Math.max(Y_RANGE_MIN_PAD, (yMax - yMin) * Y_RANGE_PAD_RATIO);
  return [yMin - yPad, yMax + yPad];
}

function optionBoundsKey(strikePrices) {
  const finitePrices = (strikePrices || [])
    .map((value) => toFiniteNumber(value))
    .filter((value) => value !== null);
  if (finitePrices.length === 0) {
    return "none";
  }
  const yMin = Math.min(...finitePrices);
  const yMax = Math.max(...finitePrices);
  return `${yMin.toFixed(6)}|${yMax.toFixed(6)}`;
}

function isPriceNearOrOutsideYEdge(yRange, stockPrice) {
  if (!Array.isArray(yRange) || yRange.length !== 2) {
    return false;
  }
  const priceNum = toFiniteNumber(stockPrice);
  if (priceNum === null) {
    return false;
  }
  const yMin = Math.min(Number(yRange[0]), Number(yRange[1]));
  const yMax = Math.max(Number(yRange[0]), Number(yRange[1]));
  const span = yMax - yMin;
  if (!Number.isFinite(span) || span <= 0) {
    return true;
  }
  const innerMin = yMin + span * Y_RANGE_EDGE_TRIGGER_RATIO;
  const innerMax = yMax - span * Y_RANGE_EDGE_TRIGGER_RATIO;
  return priceNum <= innerMin || priceNum >= innerMax;
}

function mergeExpandedYRange(currentRange, targetRange) {
  if (!Array.isArray(currentRange) || currentRange.length !== 2) {
    return targetRange;
  }
  if (!Array.isArray(targetRange) || targetRange.length !== 2) {
    return currentRange;
  }
  const currentMin = Math.min(Number(currentRange[0]), Number(currentRange[1]));
  const currentMax = Math.max(Number(currentRange[0]), Number(currentRange[1]));
  const targetMin = Math.min(Number(targetRange[0]), Number(targetRange[1]));
  const targetMax = Math.max(Number(targetRange[0]), Number(targetRange[1]));
  return [Math.min(currentMin, targetMin), Math.max(currentMax, targetMax)];
}

function pickPanelYRange(chartId, strikePrices, stockPrice) {
  const boundsKey = optionBoundsKey(strikePrices);
  const targetRange = computePanelYRange(strikePrices, stockPrice);
  const prevState = panelYRangeState.get(chartId);

  let nextRange = targetRange;
  if (prevState && Array.isArray(prevState.range) && targetRange) {
    const optionsBoundsChanged = prevState.boundsKey !== boundsKey;
    if (!optionsBoundsChanged) {
      if (isPriceNearOrOutsideYEdge(prevState.range, stockPrice)) {
        nextRange = mergeExpandedYRange(prevState.range, targetRange);
      } else {
        nextRange = prevState.range;
      }
    }
  }

  if (nextRange) {
    panelYRangeState.set(chartId, { range: nextRange, boundsKey });
  } else {
    panelYRangeState.delete(chartId);
  }
  return nextRange;
}

function legendThresholdText(threshold) {
  const value = Number(threshold);
  if (!Number.isFinite(value)) {
    return "--";
  }
  return `${Math.round(value)}%`;
}

function markerSvg(shape, edgeColor, filled = false) {
  const fillColor = filled ? edgeColor : LEGEND_HOLLOW_COLOR;
  if (shape === "triangle") {
    return `
      <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <polygon
          points="8,1.8 14,13.6 2,13.6"
          fill="${fillColor}"
          stroke="${edgeColor}"
          stroke-width="1.6"
          stroke-linejoin="round"
        />
      </svg>
    `;
  }
  return `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <circle
        cx="8"
        cy="8"
        r="5.5"
        fill="${fillColor}"
        stroke="${edgeColor}"
        stroke-width="1.8"
      />
    </svg>
  `;
}

function legendItem(iconHtml, label) {
  return `
    <div class="legend-item">
      <span class="legend-icon">${iconHtml}</span>
      <span class="legend-label">${label}</span>
    </div>
  `;
}

function legendFilledItem(thresholdText) {
  return `
    <div class="legend-item">
      <span class="legend-icon legend-icon-pair">
        ${markerSvg("circle", LEGEND_SHORT_COLOR, true)}
        ${markerSvg("triangle", LEGEND_SHORT_COLOR, true)}
      </span>
      <span class="legend-label">profit% &gt;= ${thresholdText}</span>
    </div>
  `;
}

function updateLegend(threshold) {
  const legend = document.getElementById("legend");
  if (!legend) {
    return;
  }
  const thresholdText = legendThresholdText(threshold);
  if (legend.dataset.thresholdText === thresholdText) {
    return;
  }
  legend.dataset.thresholdText = thresholdText;
  legend.innerHTML = [
    legendItem(markerSvg("circle", LEGEND_SHORT_COLOR), "Short Call"),
    legendItem(markerSvg("circle", LEGEND_LONG_COLOR), "Long Call"),
    legendItem(markerSvg("triangle", LEGEND_SHORT_COLOR), "Short Put"),
    legendItem(markerSvg("triangle", LEGEND_LONG_COLOR), "Long Put"),
    legendFilledItem(thresholdText),
  ].join("");
}

function updateHeader(snapshot) {
  const header = snapshot.header || {};
  const titleText =
    typeof header.title === "string" && header.title.trim()
      ? header.title
      : "Option Positions Dashboard";
  const titleEl = document.getElementById("title");
  if (titleEl) {
    titleEl.textContent = titleText;
  }
  if (titleText) {
    document.title = titleText;
  }

  const statusEl = document.getElementById("status");
  if (!statusEl) {
    return;
  }
  if (typeof header.status_text === "string" && header.status_text.trim()) {
    statusEl.textContent = header.status_text;
    return;
  }
  const generatedAt = new Date(snapshot.generated_at).toLocaleString();
  const optionsDone = formatOptionsDoneTimes(snapshot);
  const priceDone = formatLoadedTime(snapshot.price_done_at);
  statusEl.textContent =
    `updated: ${generatedAt} | options_loaded=${optionsDone} | price_loaded=${priceDone}`;
}

function updateServerSettings(snapshot) {
  const el = document.getElementById("server-settings");
  if (!el) {
    return;
  }
  if (typeof snapshot.server_settings_text === "string" && snapshot.server_settings_text.trim()) {
    el.textContent = snapshot.server_settings_text;
    return;
  }
  const s = snapshot.server_settings || {};
  const stockCodes = Array.isArray(s.stock_codes) ? s.stock_codes.join(",") : "-";
  const futuPorts = Array.isArray(s.futu_ports) ? s.futu_ports.join(",") : "-";
  const startedAt = s.started_at ? new Date(s.started_at).toLocaleString() : "-";
  el.textContent =
    `server settings: started_at=${startedAt} | stock_codes=${stockCodes} | ` +
    `futu_host=${s.futu_host ?? "-"} futu_ports=${futuPorts} | ` +
    `poll_interval=${s.poll_interval ?? "-"}s price_interval=${s.price_interval ?? "-"}s ui_interval=${s.ui_interval ?? "-"}s | ` +
    `price_mode=${s.price_mode ?? "-"} | web=${s.web_host ?? "-"}:${s.web_port ?? "-"}`;
}

function buildGrid(snapshot) {
  exitFullscreenCard();
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  panelYRangeState.clear();
  const cols = Math.max(1, snapshot.ports.length);
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(340px, 1fr))`;

  const panelMap = new Map();
  snapshot.panels.forEach((panel) => {
    panelMap.set(`${panel.port_index}|${panel.stock_code}`, panel);
  });

  snapshot.stock_codes.forEach((stockCode) => {
    snapshot.ports.forEach((_, portIndex) => {
      const panel = panelMap.get(`${portIndex}|${stockCode}`);
      const card = document.createElement("div");
      card.className = "card";
      const chart = document.createElement("div");
      chart.className = "chart";
      chart.id = panelId(portIndex, stockCode);
      card.appendChild(createFullscreenButton(card, chart.id));
      card.appendChild(chart);
      grid.appendChild(card);
      if (panel) {
        renderPanel(chart.id, panel);
      }
    });
  });
}

function renderPanel(id, panel) {
  const isLeftColumn = Number(panel.port_index || 0) === 0;
  const options = panel.options || [];
  const hasOptions = options.length > 0;
  const countText =
    typeof panel.position_count_text === "string" && panel.position_count_text.trim()
      ? panel.position_count_text.trim()
      : `shares=${formatQuantity(panel.stock_share_count)} | short call: 0 | short put: 0 | long call: 0 | long put: 0`;
  const xVals = options.map((o) => o.strike_date);
  const yVals = options.map((o) => o.strike_price);
  const sizes = options.map((o) => o.marker_size);
  const symbols = options.map((o) => o.marker_symbol);
  const lineColors = options.map((o) => o.marker_line_color);
  const fillColors = options.map((o) => o.marker_fill_color);
  const hoverTexts = options.map((o) => o.hover_text);
  const groupedHoverTexts = buildGroupedHoverTexts(options);

  const uniqueDates = Array.from(new Set(xVals)).sort();
  let xRange;
  if (uniqueDates.length > 0) {
    const times = uniqueDates.map((d) => new Date(d).getTime());
    const xMin = Math.min(...times);
    const xMax = Math.max(...times);
    const dayMs = 24 * 60 * 60 * 1000;
    const xPad = Math.max(dayMs, (xMax - xMin) * 0.1);
    xRange = [new Date(xMin - xPad), new Date(xMax + xPad)];
  }

  const yRange = pickPanelYRange(id, yVals, panel.stock_price);

  const traces = [];
  if (hasOptions) {
    traces.push({
      type: "scatter",
      mode: "markers",
      x: xVals,
      y: yVals,
      text: hoverTexts,
      customdata: groupedHoverTexts,
      hoverinfo: "none",
      marker: {
        size: sizes,
        symbol: symbols,
        color: fillColors,
        line: { color: lineColors, width: 1.8 },
      },
      hoverlabel: {
        bgcolor: "rgba(255,255,255,0.94)",
        bordercolor: "#94a3b8",
        font: { color: "#0f172a", size: 12 },
        align: "left",
      },
      showlegend: false,
    });
  }

  const shapes = [];
  const annotations = [];
  if (panel.stock_price !== null && panel.stock_price !== undefined) {
    const y0 = Number(panel.stock_price.toFixed(2));
    shapes.push({
      type: "line",
      xref: "paper",
      x0: 0,
      x1: 1,
      y0,
      y1: y0,
      line: { color: "red", width: 1, dash: "dash" },
    });
    annotations.push({
      xref: "paper",
      x: isLeftColumn ? 0.01 : 0.99,
      y: y0,
      xanchor: isLeftColumn ? "left" : "right",
      yanchor: "middle",
      text: y0.toFixed(2),
      showarrow: false,
      font: { color: "red", size: 11 },
      bgcolor: "#ffffff",
      borderpad: 1,
      yshift: 10,
    });
  }

  if (!hasOptions) {
    annotations.push({
      xref: "paper",
      yref: "paper",
      x: 0.5,
      y: 0.5,
      text: "No option positions",
      showarrow: false,
      font: { color: "#6b7280", size: 14 },
    });
  }

  annotations.push({
    xref: "paper",
    yref: "paper",
    x: 0.995,
    y: 0.015,
    xanchor: "right",
    yanchor: "bottom",
    text: countText,
    showarrow: false,
    font: { color: "#334155", size: 11 },
    align: "right",
    bgcolor: "rgba(255,255,255,0.82)",
    bordercolor: "#cbd5e1",
    borderwidth: 1,
    borderpad: 3,
  });

  const layout = {
    template: "none",
    title: { text: panel.title, x: 0.5, xanchor: "center", font: { size: 14 } },
    margin: isLeftColumn
      ? { l: 70, r: 20, t: 58, b: 92 }
      : { l: 36, r: 70, t: 58, b: 92 },
    xaxis: {
      // Plotly falls back to epoch dates when a date axis has no data. Hide the
      // empty x-axis so a price-only panel does not display bogus 2000 dates.
      visible: hasOptions,
      title: hasOptions
        ? { text: "Strike Date", font: { size: 14 }, standoff: 10 }
        : undefined,
      type: hasOptions ? "date" : "linear",
      tickmode: hasOptions ? "array" : "auto",
      tickvals: hasOptions ? uniqueDates : undefined,
      ticktext: hasOptions ? uniqueDates : undefined,
      range: hasOptions ? xRange : [0, 1],
      tickangle: -45,
      automargin: false,
      tickfont: { size: 12 },
      gridcolor: "#edf2f7",
    },
    yaxis: {
      title: isLeftColumn ? { text: "Strike Price", font: { size: 14 } } : undefined,
      side: isLeftColumn ? "left" : "right",
      range: yRange,
      automargin: true,
      tickfont: { size: 12 },
      gridcolor: "#edf2f7",
    },
    shapes,
    annotations,
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
  };

  const config = { responsive: true, displaylogo: false };
  const chartEl = document.getElementById(id);
  Plotly.react(id, traces, layout, config).then(() => {
    bindOptionTooltip(chartEl);
    bindHoverLabelClamp(chartEl);
    scheduleHoverLabelClamp(chartEl);
  });
}

function ensureRefreshTimer(snapshot) {
  const nextMs = Math.max(1000, Number(snapshot.ui_interval || 1) * 1000);
  if (refreshTimer && nextMs === refreshMs) {
    return;
  }
  refreshMs = nextMs;
  if (refreshTimer) {
    clearInterval(refreshTimer);
  }
  refreshTimer = setInterval(refresh, refreshMs);
}

function formatOptionsDoneTimes(snapshot) {
  const doneByPort = snapshot.options_done_at_by_port || {};
  const ports = snapshot.ports || [];
  const parts = ports.map((port) => {
    const iso = doneByPort[String(port)] ?? doneByPort[port];
    if (!iso) {
      return `${port}:-`;
    }
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) {
      return `${port}:-`;
    }
    return `${port}:${dt.toLocaleTimeString()}`;
  });
  return parts.join(", ");
}

function formatLoadedTime(iso) {
  if (!iso) {
    return "-";
  }
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) {
    return "-";
  }
  return dt.toLocaleTimeString();
}

async function refresh() {
  try {
    const resp = await fetch("/api/snapshot", { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const snapshot = await resp.json();

    ensureRefreshTimer(snapshot);
    updateHeader(snapshot);
    updateLegend(snapshot.profit_highlight_threshold);
    updateServerSettings(snapshot);

    if (!initialized) {
      buildGrid(snapshot);
      initialized = true;
    } else {
      snapshot.panels.forEach((panel) => {
        renderPanel(panelId(panel.port_index, panel.stock_code), panel);
      });
    }

  } catch (err) {
    const status = document.getElementById("status");
    status.textContent = `refresh failed: ${err}`;
  }
}

refresh();

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    exitFullscreenCard();
  }
});

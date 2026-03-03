let initialized = false;
let refreshTimer = null;
let refreshMs = 1000;
const fullscreenState = {
  card: null,
  chartId: null,
  button: null,
  backdrop: null,
};

function panelId(portIndex, stockCode) {
  return `panel-${portIndex}-${stockCode.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
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

function updateLegend(threshold) {
  const legend = document.getElementById("legend");
  if (!legend) {
    return;
  }
  const shown = threshold === null || threshold === undefined ? "--" : threshold;
  legend.textContent =
    `Shape: circle=CALL, triangle=PUT | Edge color: green=SHORT, pink=LONG | Filled marker: profit% >= ${shown}`;
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
  const options = panel.options || [];
  const xVals = options.map((o) => o.strike_date);
  const yVals = options.map((o) => o.strike_price);
  const sizes = options.map((o) => o.marker_size);
  const symbols = options.map((o) => o.marker_symbol);
  const lineColors = options.map((o) => o.marker_line_color);
  const fillColors = options.map((o) => o.marker_fill_color);
  const hoverTexts = options.map((o) => o.hover_text);

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

  const yCandidates = [...yVals];
  if (panel.stock_price !== null && panel.stock_price !== undefined) {
    yCandidates.push(Number(panel.stock_price));
  }

  let yRange;
  if (yCandidates.length > 0) {
    const yMin = Math.min(...yCandidates);
    const yMax = Math.max(...yCandidates);
    const yPad = (yMax - yMin) * 0.1 || 1.0;
    yRange = [yMin - yPad, yMax + yPad];
  }

  const traces = [];
  if (options.length > 0) {
    traces.push({
      type: "scatter",
      mode: "markers",
      x: xVals,
      y: yVals,
      text: hoverTexts,
      hovertemplate: "%{text}<extra></extra>",
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
      x: 0,
      y: y0,
      xanchor: "right",
      yanchor: "middle",
      text: `y=${y0.toFixed(2)}`,
      showarrow: false,
      xshift: -8,
      font: { color: "red", size: 11 },
    });
  }

  if (options.length === 0) {
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

  const layout = {
    template: "none",
    title: { text: panel.title, x: 0.01, xanchor: "left", font: { size: 14 } },
    margin: { l: 70, r: 20, t: 58, b: 82 },
    xaxis: {
      title: { text: "Strike Date", font: { size: 14 } },
      type: "date",
      tickmode: uniqueDates.length > 0 ? "array" : "auto",
      tickvals: uniqueDates.length > 0 ? uniqueDates : undefined,
      ticktext: uniqueDates.length > 0 ? uniqueDates : undefined,
      range: xRange,
      tickangle: -45,
      automargin: true,
      tickfont: { size: 12 },
      gridcolor: "#edf2f7",
    },
    yaxis: {
      title: { text: "Strike Price", font: { size: 14 } },
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
  Plotly.react(id, traces, layout, config);
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

async function refresh() {
  try {
    const resp = await fetch("/api/snapshot", { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const snapshot = await resp.json();

    ensureRefreshTimer(snapshot);
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

    const status = document.getElementById("status");
    const generatedAt = new Date(snapshot.generated_at).toLocaleString();
    const optionsDone = formatOptionsDoneTimes(snapshot);
    status.textContent =
      `updated: ${generatedAt} | options_v=${snapshot.versions.options} price_v=${snapshot.versions.price} | options_done=${optionsDone}`;
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

let initialized = false;
let refreshTimer = null;
let refreshMs = 1000;

function panelId(portIndex, stockCode) {
  return `panel-${portIndex}-${stockCode.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
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

function buildGrid(snapshot) {
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

async function refresh() {
  try {
    const resp = await fetch("/api/snapshot", { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const snapshot = await resp.json();

    ensureRefreshTimer(snapshot);
    updateLegend(snapshot.profit_highlight_threshold);

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
    status.textContent =
      `updated: ${generatedAt} | options_v=${snapshot.versions.options} price_v=${snapshot.versions.price} | mode=${snapshot.price_mode}`;
  } catch (err) {
    const status = document.getElementById("status");
    status.textContent = `refresh failed: ${err}`;
  }
}

refresh();

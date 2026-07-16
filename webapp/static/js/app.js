/* ════════════════════════════════════════════════════════════════
   GHRSST MUR SST — Frontend Logic
   Marine Environmental Research, Fisheries Research Institute, MOA
   ════════════════════════════════════════════════════════════════ */

// ── Constants ───────────────────────────────────────────────
// Tropical West/Central Pacific tuna grounds: 20°S–20°N, 130°E–150°W.
// Longitude uses the 0–360 convention (150°W = 210) so the dateline-
// crossing region is one contiguous interval.
const LON_MIN = 130.0, LON_MAX = 210.0;
const LAT_MIN = -20.0, LAT_MAX = 20.0;
// Convert a 0–360 longitude to a friendly E/W tick label (e.g. 210 → 150°W).
function lonLabel(x) {
  const v = x > 180 ? x - 360 : x;
  return v < 0 ? `${(-v).toFixed(0)}°W` : `${v.toFixed(0)}°E`;
}

const OCEAN_COLORSCALE = [
  [0.00, "#030085"],
  [0.10, "#0028c8"],
  [0.22, "#0066ff"],
  [0.32, "#00b4e6"],
  [0.42, "#00e6b4"],
  [0.52, "#80ff80"],
  [0.62, "#ffff00"],
  [0.72, "#ffaa00"],
  [0.82, "#ff4400"],
  [0.92, "#cc0000"],
  [1.00, "#660000"],
];

// HSI 漁場預測機率配色（低→高：深藍→藍→黃→橙→紅）
const HSI_COLORSCALE = [
  [0.00, "#0b1f3a"],
  [0.05, "#1e3a5f"],
  [0.25, "#38bdf8"],
  [0.50, "#facc15"],
  [0.75, "#f97316"],
  [1.00, "#b91c1c"],
];

// ── DOM refs ────────────────────────────────────────────────
const $  = (id) => document.getElementById(id);
const plotDiv     = $("plot");
const cursorInfo  = $("cursorInfo");
const ciLon       = $("ciLon");
const ciLat       = $("ciLat");
const ciSst       = $("ciSst");
const ciLabel     = $("ciLabel");
const statusBar   = $("statusBar");
const loadingOv   = $("loadingOverlay");
const loadingTxt  = $("loadingText");
const emptyState  = $("emptyState");
const logBox      = $("logBox");
const connStatus  = $("connectionStatus");

// ── State ───────────────────────────────────────────────────
const state = {
  sst: null,            // {lon: [...], lat: [...], values: [[...]], stats, date, factor}
  fronts: null,         // {lons: [...], lats: [...]}
  coastline: null,      // [[ [lon,lat], ... ], ...]
  showIsotherm: true,
  showCoastline: true,
  showFronts: false,
  isoInterval: 2.0,
  resolution: 220000,
  logCursor: 0,
  downloadPolling: null,
  frontsPolling: null,
  // time series
  seriesMeta: null,
  seriesIndex: 0,
  seriesPlaying: false,
  seriesTimer: null,
  seriesCache: new Map(),
  seriesPolling: null,
  // stations
  stations: [],
  // analysis tools
  profileMode: false,
  profilePts: [],
  profileLine: null,
  chlOverlay: null,
  chlScale: 1.0,
  currents: null,
  vecScale: 0.55,
  vecDensity: 3600,
  datasets: [],
  // habitat / fishing-ground prediction
  hsiParams: null,
  hsiInfo: null,
};

// ── Utilities ───────────────────────────────────────────────
function showLoading(text) {
  loadingTxt.textContent = text || "處理中…";
  loadingOv.classList.remove("hidden");
}
function hideLoading() { loadingOv.classList.add("hidden"); }

function setStatus(msg) { statusBar.textContent = msg; }
function setConnBusy(on, text) {
  if (on) {
    connStatus.classList.add("busy");
    connStatus.querySelector(".pill-text").textContent = text || "處理中";
  } else {
    connStatus.classList.remove("busy");
    connStatus.querySelector(".pill-text").textContent = text || "系統就緒";
  }
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

// ── Log polling ─────────────────────────────────────────────
async function pollLogs() {
  try {
    const data = await api(`/api/logs?after=${state.logCursor}`);
    if (data.logs && data.logs.length) {
      logBox.textContent += data.logs.join("\n") + "\n";
      logBox.scrollTop = logBox.scrollHeight;
      state.logCursor = data.total;
    }
  } catch (_) { /* ignore */ }
}
setInterval(pollLogs, 800);

// ── Plot rendering ──────────────────────────────────────────
function buildTraces() {
  if (!state.sst) return [];

  const traces = [];

  // ── Fishing-ground prediction (HSI probability) ──────────────
  if (state.sst.kind === "hsi") {
    const s = state.sst;
    const cd = s.values.map((row, i) => row.map((_, j) => [
      (s.sstGrid && s.sstGrid[i]) ? s.sstGrid[i][j] : null,
      (s.chlGrid && s.chlGrid[i]) ? s.chlGrid[i][j] : null,
      (s.sshaGrid && s.sshaGrid[i]) ? s.sshaGrid[i][j] : null,
    ]));
    traces.push({
      type: "heatmap",
      x: s.lon, y: s.lat, z: s.values,
      colorscale: HSI_COLORSCALE, zmin: 0, zmax: 1,
      zsmooth: "best", hoverongaps: false,
      customdata: cd,
      hovertemplate:
        "<b>棲地機率 HSI</b> %{z:.2f}<br>" +
        "SST %{customdata[0]:.2f}°C · Chl %{customdata[1]:.3f} · SSHA %{customdata[2]:.2f}<br>" +
        "%{x:.2f}°E, %{y:.2f}°N<extra></extra>",
      colorbar: {
        title: { text: "棲地適合度機率 HSI", font: { color: "#e2e8f0", size: 12 } },
        tickvals: [0, 0.25, 0.5, 0.75, 1],
        ticktext: ["0", "0.25 低", "0.5 中", "0.75 高", "1 最適"],
        tickfont: { color: "#cbd5e1", size: 11 },
        outlinecolor: "#14b8a6", outlinewidth: 1, thickness: 14, len: 0.85, x: 1.005,
      },
      showscale: true, name: "HSI",
    });
    if (state.showCoastline && state.coastline && state.coastline.length) {
      const xs = [], ys = [];
      state.coastline.forEach(poly => {
        poly.forEach(([x, y]) => { xs.push(x); ys.push(y); });
        xs.push(null); ys.push(null);
      });
      traces.push({
        type: "scattergl", mode: "lines", x: xs, y: ys,
        line: { color: "#6aaf6a", width: 1.0 },
        hoverinfo: "skip", showlegend: false, name: "Coastline",
      });
    }
    return traces;
  }

  // Heatmap: 距平/SSH → 發散；chl → log Viridis；speed → Plasma；DHW → 熱壓力序列
  const isAnom = !!state.sst.anomaly;
  const isChl = state.sst.kind === "chl" && !isAnom;
  const isSsh = state.sst.kind === "ssh" && !isAnom;
  const isSpd = state.sst.kind === "speed" && !isAnom;
  const isMuranom = state.sst.kind === "anom" && !isAnom;   // 官方距平資料集
  const isDhw = state.sst.kind === "dhw" && !isAnom;         // 累積熱壓力
  const isDiv = isAnom || isSsh || isMuranom;               // 發散、置中 0
  const zAbs = isDiv ? Math.max(
    Math.abs(state.sst.stats.min ?? 0.1),
    Math.abs(state.sst.stats.max ?? 0.1), isSsh ? 0.1 : 0.5) : null;
  // Coral Reef Watch DHW 配色（0=白，愈高愈紅紫）
  const DHW_COLORSCALE = [
    [0.0, "#ffffff"], [0.2, "#fce94f"], [0.4, "#f57900"],
    [0.6, "#cc0000"], [0.8, "#5c0000"], [1.0, "#2e0854"],
  ];
  let zvals = state.sst.values;
  let chlExtra = {};
  if (isChl) {
    zvals = state.sst.values.map(row =>
      row.map(v => (v == null || v <= 0) ? null : Math.log10(v)));
    chlExtra = {
      zmin: Math.log10(0.05), zmax: Math.log10(20),
      customdata: state.sst.values,
      hovertemplate: "<b>Chl-a</b> %{customdata:.3f} mg/m³<br>" +
        "經度 %{x:.3f}°E<br>緯度 %{y:.3f}°N<extra></extra>",
    };
  }
  const dhwMax = Math.max(8, state.sst.stats.max ?? 8);
  const sstUnit = (state.sst.unit === "K") ? "K" : "°C";   // OSTIA 原始為 K
  const hoverTxt = isMuranom
      ? "<b>距平</b> %{z:.2f} °C<br>"
      : (isDhw ? "<b>DHW</b> %{z:.2f} °C-週<br>" : `<b>SST</b> %{z:.2f} ${sstUnit}<br>`);
  traces.push({
    type: "heatmap",
    x: state.sst.lon,
    y: state.sst.lat,
    z: zvals,
    colorscale: isDiv ? "RdBu"
      : (isChl ? "Viridis"
      : (isSpd ? "Plasma"
      : (isDhw ? DHW_COLORSCALE : OCEAN_COLORSCALE))),
    reversescale: isDiv,
    ...(isDiv ? { zmin: -zAbs, zmax: zAbs } : {}),
    ...(isSpd ? { zmin: 0, zmax: Math.max(0.5, state.sst.stats.max ?? 1) } : {}),
    ...(isDhw ? { zmin: 0, zmax: dhwMax } : {}),
    // 一般 SST（含 OSTIA 原始 K / 換算 °C）：圖例以資料範圍取整，配合當前單位
    ...((!isDiv && !isChl && !isSpd && !isDhw
         && state.sst.stats && state.sst.stats.min != null)
        ? { zmin: Math.floor(state.sst.stats.min),
            zmax: Math.ceil(state.sst.stats.max) } : {}),
    ...chlExtra,
    zsmooth: "best",
    hoverongaps: false,
    hovertemplate: hoverTxt +
      "經度 %{x:.3f}°E<br>緯度 %{y:.3f}°N<extra></extra>",
    colorbar: {
      title: { text: isMuranom ? "距平 ΔSST (°C)"
                 : (isAnom ? "ΔSST (°C)"
                 : (isChl ? "Chl (mg/m³)"
                 : (isSsh ? "SLA (cm)"
                 : (isSpd ? "流速 (m/s)"
                 : (isDhw ? "DHW (°C·週)" : `SST (${sstUnit})`))))),
               font: { color: "#e2e8f0", size: 12 } },
      ...(isChl ? {
        tickvals: [-1, -0.523, 0, 0.477, 1],
        ticktext: ["0.1", "0.3", "1", "3", "10"],
      } : {}),
      ...(isDhw ? {
        tickvals: [0, 4, 8, 12, 16],
        ticktext: ["0", "4 警戒", "8 嚴重", "12", "16"],
      } : {}),
      tickfont: { color: "#cbd5e1", size: 11 },
      outlinecolor: "#14b8a6",
      outlinewidth: 1,
      thickness: 14,
      len: 0.85,
      x: 1.005,
    },
    showscale: true,
    name: "SST",
  });

  // Isotherms（距平/水色/流速/DHW 底圖不畫等溫線）
  if (state.showIsotherm && state.sst.stats && !isAnom && !isChl && !isSpd
      && !isMuranom && !isDhw) {
    const interval = isSsh ? 0.1 : (parseFloat(state.isoInterval) || 2.0);
    const min = Math.ceil(state.sst.stats.min / interval) * interval;
    const max = Math.floor(state.sst.stats.max / interval) * interval;
    traces.push({
      type: "contour",
      x: state.sst.lon,
      y: state.sst.lat,
      z: state.sst.values,
      contours: {
        coloring: "none",
        showlabels: true,
        labelfont: { size: 9, color: "rgba(0,0,0,0.85)" },
        start: min, end: max, size: interval,
      },
      line: { color: "rgba(0,0,0,0.65)", width: 0.7 },
      showscale: false,
      hoverinfo: "skip",
      name: "Isotherm",
    });
  }

  // Coastlines
  if (state.showCoastline && state.coastline && state.coastline.length) {
    const xs = [], ys = [];
    state.coastline.forEach(poly => {
      poly.forEach(([x, y]) => { xs.push(x); ys.push(y); });
      xs.push(null); ys.push(null);
    });
    traces.push({
      type: "scattergl",
      mode: "lines",
      x: xs, y: ys,
      line: { color: "#6aaf6a", width: 1.0 },
      hoverinfo: "skip",
      showlegend: false,
      name: "Coastline",
    });
  }

  // Fronts
  if (state.showFronts && state.fronts && state.fronts.lons) {
    traces.push({
      type: "scattergl",
      mode: "markers",
      x: state.fronts.lons,
      y: state.fronts.lats,
      marker: {
        color: "white",
        size: 2.4,
        opacity: 0.85,
        line: { width: 0 },
      },
      hovertemplate: "<b>Front</b><br>%{x:.3f}°E, %{y:.3f}°N<extra></extra>",
      showlegend: false,
      name: "Fronts",
    });
  }

  // Chlorophyll overlay：綠色空心圓，圓越大＝濃度越高（僅顯示 ≥0.1 mg/m³）
  if (state.chlOverlay && state.chlOverlay.chl && !isChl) {
    const cs = state.chlOverlay;
    const thr = cs.threshold ?? 0.1;
    // 以 log10(chl) 線性映射到圓徑，再乘上使用者選的圓圈大小倍率
    const lmin = Math.log10(thr), lmax = Math.log10(10);
    const k = state.chlScale || 1.0;
    const sizes = cs.chl.map(v => {
      const t = Math.max(0, Math.min(1, (Math.log10(v) - lmin) / (lmax - lmin)));
      return (6 + t * 18) * k;
    });
    traces.push({
      type: "scattergl", mode: "markers",
      x: cs.lons, y: cs.lats,
      marker: {
        symbol: "circle-open",                    // 空心圓
        size: sizes,
        color: "#22c55e",                          // 綠色圈線
        line: { color: "#22c55e", width: 1.6 },   // 圈線粗細
        opacity: 0.9,
      },
      customdata: cs.chl,
      hovertemplate: "<b>葉綠素-a</b> %{customdata:.3f} mg/m³<br>" +
        "%{x:.2f}°E, %{y:.2f}°N<extra>VIIRS 水色</extra>",
      showlegend: false, name: "Chl",
    });
  }

  // Transect line
  if (state.profileLine) {
    traces.push({
      type: "scattergl",
      mode: "lines+markers",
      x: state.profileLine.lons,
      y: state.profileLine.lats,
      line: { color: "#f472b6", width: 2.5, dash: "dot" },
      marker: { size: 8, color: "#f472b6", symbol: "circle-open" },
      hoverinfo: "skip",
      showlegend: false,
      name: "Transect",
    });
  }

  // Surface current vectors（箭頭：桿+箭頭側翼）
  if (state.currents) {
    const cs = state.currents;
    const SCALE = state.vecScale;       // 度 / (m/s)，由「箭頭大小」選項控制
    const xs = [], ys = [];
    for (let k = 0; k < cs.lons.length; k++) {
      const x0 = cs.lons[k], y0 = cs.lats[k];
      const dx = cs.u[k] * SCALE, dy = cs.v[k] * SCALE;
      const x1 = x0 + dx, y1 = y0 + dy;
      xs.push(x0, x1, null); ys.push(y0, y1, null);
      // 箭頭側翼（±150°，長度 30%）
      const ang = Math.atan2(dy, dx), L = Math.hypot(dx, dy) * 0.3;
      xs.push(x1, x1 - L * Math.cos(ang - 0.45), null);
      ys.push(y1, y1 - L * Math.sin(ang - 0.45), null);
      xs.push(x1, x1 - L * Math.cos(ang + 0.45), null);
      ys.push(y1, y1 - L * Math.sin(ang + 0.45), null);
    }
    traces.push({
      type: "scattergl", mode: "lines",
      x: xs, y: ys,
      line: { color: "rgba(255,255,255,0.85)",
              width: state.vecDensity >= 8000 ? 0.8 : 1.1 },
      hoverinfo: "skip", showlegend: false, name: "CurrentsArrows",
    });
    traces.push({
      type: "scattergl", mode: "markers",
      x: cs.lons, y: cs.lats,
      marker: { size: state.vecDensity >= 8000 ? 2.2 : 3.2,
                color: "rgba(255,255,255,0.65)" },
      customdata: cs.lons.map((_, k) => [cs.speed[k], cs.dir[k]]),
      hovertemplate: "<b>流速</b> %{customdata[0]:.2f} m/s<br>" +
        "<b>流向</b> %{customdata[1]:.0f}°（北=0 順時針）<br>" +
        "%{x:.2f}°E, %{y:.2f}°N<extra>地轉流</extra>",
      showlegend: false, name: "CurrentsPts",
    });
  }

  // Stations overlay
  if (state.stations && state.stations.length) {
    traces.push({
      type: "scattergl",
      mode: "markers+text",
      x: state.stations.map(s => s.lon),
      y: state.stations.map(s => s.lat),
      text: state.stations.map(s => s.name),
      textposition: "top center",
      textfont: { color: "#fbbf24", size: 10 },
      marker: { symbol: "star", color: "#fbbf24", size: 11,
                line: { color: "#000", width: 1 } },
      hovertemplate: "<b>%{text}</b><br>%{x:.3f}°E, %{y:.3f}°N<extra></extra>",
      showlegend: false,
      name: "Stations",
    });
  }

  return traces;
}

// 依目前顯示的圖層自動決定標題（SST／Chl-a／SSHA／漁場預測…）
function plotTitleText() {
  const s = state.sst;
  if (!s) return "";
  const dateSpan = `　<span style="color:#94a3b8">${s.date || ""}</span>`;
  if (s.kind === "hsi") {
    return `<b>${s.name_zh || ""}漁場預測 (ECDF-HSI)</b>${dateSpan}`;
  }
  if (s.anomaly) return `<b>海面水溫距平 ΔSST (°C)</b>${dateSpan}`;
  if (s.kind === "sst") {
    const lbl = s.unit === "K"
      ? "海面溫度 SST (K)｜OSTIA 原始"
      : "海面水溫 SST (°C)｜OSTIA";
    return `<b>${lbl}</b>${dateSpan}`;
  }
  const labels = {
    sst: "海面水溫 SST (°C)｜OSTIA",
    chl: "葉綠素-a Chl-a (mg/m³)｜GlobColour",
    ssh: "海面高度距平 SSHA (cm)｜DUACS",
    speed: "表面流速 (m/s)",
    anom: "海面水溫距平 ΔSST (°C)",
    dhw: "累積熱壓力 DHW (°C·週)",
  };
  return `<b>${labels[s.kind] || "海面水溫 SST (°C)"}</b>${dateSpan}`;
}

function plotLayout(preserveAxes = false) {
  const layout = {
    paper_bgcolor: "#050d1a",
    plot_bgcolor:  "#07172e",
    margin: { t: 50, b: 50, l: 56, r: 26 },
    title: state.sst ? {
      text: plotTitleText(),
      font: { color: "#e2e8f0", size: 14, family: "Inter, Noto Sans TC" },
      x: 0.5, xanchor: "center",
    } : "",
    xaxis: {
      title: { text: "經度 Longitude", font: { color: "#cbd5e1", size: 11 } },
      tickfont: { color: "#cbd5e1", size: 10 },
      gridcolor: "rgba(42, 84, 138, 0.4)",
      zerolinecolor: "rgba(42, 84, 138, 0.4)",
      linecolor: "#14b8a6",
      mirror: true,
      showline: true,
      tickmode: "array",
      tickvals: (() => { const t = []; for (let x = 130; x <= 210; x += 10) t.push(x); return t; })(),
      ticktext: (() => { const t = []; for (let x = 130; x <= 210; x += 10) t.push(lonLabel(x)); return t; })(),
    },
    yaxis: {
      title: { text: "緯度 Latitude (°N)", font: { color: "#cbd5e1", size: 11 } },
      tickfont: { color: "#cbd5e1", size: 10 },
      gridcolor: "rgba(42, 84, 138, 0.4)",
      zerolinecolor: "rgba(42, 84, 138, 0.4)",
      linecolor: "#14b8a6",
      mirror: true,
      showline: true,
      scaleanchor: "x",
      scaleratio: 1,
      dtick: 5,
    },
    hovermode: "closest",
    showlegend: false,
    dragmode: "zoom",
    annotations: state.sst ? [{
      text: "資料來源 NASA JPL · MUR v4.1 · 農業部水產試驗所 漁海況研究小組",
      x: 0, y: -0.07, xref: "paper", yref: "paper",
      showarrow: false, xanchor: "left",
      font: { size: 9, color: "#475569", family: "Inter" },
    }] : [],
  };

  if (!preserveAxes && state.sst) {
    layout.xaxis.range = [LON_MIN, LON_MAX];
    layout.yaxis.range = [LAT_MIN, LAT_MAX];
  }
  return layout;
}

const plotConfig = {
  responsive: true,
  displaylogo: false,
  scrollZoom: true,
  toImageButtonOptions: {
    format: "png",
    filename: "GHRSST_MUR_SST",
    height: 1100,
    width: 1400,
    scale: 2,
  },
  modeBarButtonsToRemove: ["lasso2d", "select2d"],
};

async function drawPlot(preserveAxes = false) {
  if (!state.sst) {
    Plotly.purge(plotDiv);
    emptyState.classList.remove("hidden");
    return;
  }
  emptyState.classList.add("hidden");

  // 圖層／單位切換（如 OSTIA 由 K 換算成 °C）時強制重繪，
  // 否則 Plotly.react 會沿用舊的色標圖例（zmin/zmax 不更新）。
  const rkey = `${state.sst.kind}|${state.sst.unit || ""}|${!!state.sst.anomaly}`;
  if (drawPlot._lastKey !== undefined && drawPlot._lastKey !== rkey) {
    Plotly.purge(plotDiv);
    plotDiv.__stationClickWired = false;
  }
  drawPlot._lastKey = rkey;

  // Get current axis ranges if preserving
  let preservedX = null, preservedY = null;
  if (preserveAxes && plotDiv.layout) {
    preservedX = plotDiv.layout.xaxis.range;
    preservedY = plotDiv.layout.yaxis.range;
  }

  const traces = buildTraces();
  const layout = plotLayout(preserveAxes);
  if (preservedX && preservedY) {
    layout.xaxis.range = preservedX;
    layout.yaxis.range = preservedY;
  }

  await Plotly.react(plotDiv, traces, layout, plotConfig);


  // Hover handler for the floating cursor info
  plotDiv.on("plotly_hover", ev => {
    const p = ev.points && ev.points[0];
    if (!p || p.data.type !== "heatmap") return;
    cursorInfo.classList.add("visible");
    const info = layerInfo();          // 依目前圖層決定標籤／單位／數值換算
    const noData = (state.sst && state.sst.kind === "hsi") ? "無資料" : "陸地/遮罩";
    if (ciLabel) ciLabel.textContent = info.label;   // 標籤配合目前圖層
    ciLon.textContent = lonLabel(p.x);
    ciLat.textContent = p.y.toFixed(3) + "°N";
    ciSst.textContent = (p.z == null) ? noData : info.fmt(p.z);
    setStatus(
      `經度 ${lonLabel(p.x)} │ 緯度 ${p.y.toFixed(3)}°N │ ` +
      `${info.statusLabel} ${p.z == null ? "—" : info.fmt(p.z)}`
    );
  });
  if (!plotDiv.__stationClickWired) {
    plotDiv.__stationClickWired = true;
    plotDiv.on("plotly_click", (ev) => {
      const pt = ev.points && ev.points[0];
      if (!pt || typeof pt.x !== "number") return;
      if (state.profileMode) { handleProfileClick(pt.x, pt.y); return; }
      const chk = document.getElementById("chkAddStation");
      if (!chk || !chk.checked) return;
      $("stLat").value = pt.y.toFixed(3);
      $("stLon").value = pt.x.toFixed(3);
      setStatus(`已選點位 ${pt.x.toFixed(3)}°E, ${pt.y.toFixed(3)}°N — 填寫站名後按「新增測站」`);
    });
  }

  plotDiv.on("plotly_unhover", () => {
    cursorInfo.classList.remove("visible");
    if (state.sst) {
      const info = layerInfo();
      const u    = info.statsUnit ? " " + info.statsUnit : "";
      const st   = state.sst.stats;
      const minV = (st.min == null) ? "—" : st.min.toFixed(2) + u;
      const maxV = (st.max == null) ? "—" : st.max.toFixed(2) + u;
      setStatus(`資料日期：${state.sst.date}　│　${info.label} 範圍：${minV} – ${maxV}`);
    }
  });
}

// ── Data fetching ───────────────────────────────────────────
async function fetchSST() {
  showLoading("讀取 SST 資料中…");
  try {
    const data = await api(`/api/sst?max_points=${state.resolution}`);
    if (!data.loaded) {
      state.sst = null;
      drawPlot();
      return;
    }
    state.sst = data;
    updateStats();
    await drawPlot();
    setStatus(`資料日期：${data.date}　│　T 範圍：${data.stats.min}–${data.stats.max} °C`);
  } catch (e) {
    appendLocalLog(`❌ 取得 SST 失敗：${e.message}`);
  } finally {
    hideLoading();
  }
}

async function fetchCoastline() {
  if (state.coastline) return;
  try {
    const data = await api("/api/coastline");
    state.coastline = data.polygons || [];
  } catch (_) { state.coastline = []; }
}

async function fetchFronts() {
  try {
    const data = await api("/api/fronts");
    if (data.available && data.lons) {
      state.fronts = { lons: data.lons, lats: data.lats };
      $("chkFronts").checked = true;
      state.showFronts = true;
      await drawPlot(true);
    }
  } catch (_) { /* ignore */ }
}

async function fetchStatus() {
  try {
    const s = await api("/api/status");
    if (s.loaded) {
      $("hdrDate").textContent = s.date;
      if (!state.sst || state.sst.filename !== s.filename) {
        await fetchSST();
        if (s.fronts_available) await fetchFronts();
      }
    }
  } catch (_) { /* ignore */ }
}

async function loadFileList() {
  try {
    const data = await api("/api/files");
    const list = $("fileList");
    if (!data.files || data.files.length === 0) {
      list.innerHTML = '<div class="muted">— 無資料 —</div>';
      return;
    }
    list.innerHTML = data.files.map(f => `
      <div class="file-item" data-name="${f.name}">
        <div class="fname">${f.name}</div>
        <div class="fmeta">${f.date || "—"}　·　${f.size_mb} MB</div>
      </div>
    `).join("");
    list.querySelectorAll(".file-item").forEach(el => {
      el.onclick = async () => {
        const name = el.dataset.name;
        showLoading(`載入 ${name} …`);
        try {
          await api("/api/load", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: name }),
          });
          state.fronts = null;
          state.showFronts = false;
          $("chkFronts").checked = false;
          await fetchSST();
        } catch (e) {
          alert("載入失敗：" + e.message);
        } finally { hideLoading(); }
      };
    });
  } catch (_) { /* ignore */ }
}

// ── Helper: per-layer display metadata ─────────────────────
/**
 * Returns display metadata for the current map layer.
 * @returns {{ label: string, unit: string, fmt: (z:number)=>string,
 *             statusLabel: string, statsUnit: string }}
 */
function layerInfo() {
  const s = state.sst;
  if (!s) return { label: "水溫", unit: "°C", fmt: z => z.toFixed(2) + " °C",
                   statusLabel: "水溫", statsUnit: "°C" };
  const kind = s.kind;
  const sstUnit = (s.unit === "K") ? "K" : "°C";

  if (kind === "hsi")
    return { label: "HSI", unit: "", fmt: z => "HSI " + z.toFixed(2),
             statusLabel: "棲地機率 HSI", statsUnit: "" };
  if (kind === "chl")
    // heatmap z stores log10(chl); real chl = 10^z
    return { label: "Chl-a", unit: "mg/m³",
             fmt: z => (Math.pow(10, z)).toFixed(3) + " mg/m³",
             statusLabel: "葉綠素-a", statsUnit: "mg/m³" };
  if (kind === "ssh")
    return { label: "SSHA", unit: "cm", fmt: z => z.toFixed(2) + " cm",
             statusLabel: "SSHA", statsUnit: "cm" };
  if (kind === "speed")
    return { label: "流速", unit: "m/s", fmt: z => z.toFixed(2) + " m/s",
             statusLabel: "流速", statsUnit: "m/s" };
  if (kind === "anom" || (kind === "sst" && s.anomaly))
    return { label: "ΔSST", unit: "°C", fmt: z => z.toFixed(2) + " °C",
             statusLabel: "SST 距平", statsUnit: "°C" };
  if (kind === "anom")   // muranom
    return { label: "距平 ΔSST", unit: "°C", fmt: z => z.toFixed(2) + " °C",
             statusLabel: "SST 距平", statsUnit: "°C" };
  if (kind === "dhw")
    return { label: "DHW", unit: "°C·週", fmt: z => z.toFixed(2) + " °C·週",
             statusLabel: "累積熱壓力 DHW", statsUnit: "°C·週" };
  // default: SST
  return { label: "水溫", unit: sstUnit, fmt: z => z.toFixed(2) + " " + sstUnit,
           statusLabel: "水溫", statsUnit: sstUnit };
}

function updateStats() {
  if (!state.sst) return;
  if (!$("statMin")) return;   // 「SST 統計」面板已移除
  const st = state.sst.stats;
  if (state.sst.kind === "hsi") {
    const f = (v) => (v == null ? "—" : v.toFixed(2));
    $("statMin").textContent  = f(st.min);
    $("statMax").textContent  = f(st.max);
    $("statMean").textContent = f(st.mean);
    $("statShape").textContent = `${state.sst.shape[0]}×${state.sst.shape[1]}`;
    $("statFactor").textContent = `最適格點 ${st.hot_cells ?? 0}`;
    return;
  }
  const info = layerInfo();
  const u = info.statsUnit ? " " + info.statsUnit : "";
  $("statMin").textContent  = st.min.toFixed(2) + u;
  $("statMax").textContent  = st.max.toFixed(2) + u;
  $("statMean").textContent = st.mean.toFixed(2) + u;
  $("statShape").textContent = `${state.sst.shape[0]}×${state.sst.shape[1]}`;
  $("statFactor").textContent = `1/${state.sst.factor}`;
}

function appendLocalLog(msg) {
  const ts = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  logBox.textContent += `[${ts}] ${msg}\n`;
  logBox.scrollTop = logBox.scrollHeight;
}

// ── Download flow ───────────────────────────────────────────
async function startDownload() {
  $("btnDownload").disabled = true;
  $("progressWrapper").classList.remove("hidden");
  $("progressFill").style.width = "0%";
  $("progressLabel").textContent = "0%";
  setConnBusy(true, "下載中");

  try {
    await api("/api/download", { method: "POST" });
  } catch (e) {
    alert("下載啟動失敗：" + e.message);
    $("btnDownload").disabled = false;
    setConnBusy(false);
    return;
  }
  if (state.downloadPolling) clearInterval(state.downloadPolling);
  state.downloadPolling = setInterval(async () => {
    try {
      const s = await api("/api/download/status");
      $("progressFill").style.width = s.progress + "%";
      $("progressLabel").textContent = s.progress.toFixed(0) + "%";
      if (!s.active) {
        clearInterval(state.downloadPolling);
        state.downloadPolling = null;
        $("btnDownload").disabled = false;
        setConnBusy(false);
        if (s.error) {
          alert("下載失敗：" + s.error);
        } else if (s.completed) {
          await fetchStatus();
          await loadFileList();
        }
        setTimeout(() => $("progressWrapper").classList.add("hidden"), 1500);
      }
    } catch (_) { /* ignore */ }
  }, 800);
}

// ── Front detection flow ────────────────────────────────────
async function startFrontDetection() {
  if (!state.sst) {
    alert("請先載入 SST 資料");
    return;
  }
  $("btnDetectFronts").disabled = true;
  setConnBusy(true, "偵測前緣中");
  showLoading("執行 Cayula-Cornillon 偵測（可能需 1-3 分鐘）…");
  try {
    await api("/api/fronts", { method: "POST" });
  } catch (e) {
    alert("啟動失敗：" + e.message);
    $("btnDetectFronts").disabled = false;
    hideLoading();
    setConnBusy(false);
    return;
  }
  if (state.frontsPolling) clearInterval(state.frontsPolling);
  state.frontsPolling = setInterval(async () => {
    try {
      const s = await api("/api/fronts");
      if (!s.active) {
        clearInterval(state.frontsPolling);
        state.frontsPolling = null;
        $("btnDetectFronts").disabled = false;
        hideLoading();
        setConnBusy(false);
        if (s.error) {
          alert("偵測失敗：" + s.error);
        } else if (s.lons) {
          state.fronts = { lons: s.lons, lats: s.lats };
          $("chkFronts").checked = true;
          state.showFronts = true;
          await drawPlot(true);
        }
      }
    } catch (_) { /* ignore */ }
  }, 1500);
}

// ── File upload ─────────────────────────────────────────────
async function uploadFile(file) {
  showLoading(`上傳並載入 ${file.name} …`);
  setConnBusy(true, "上傳中");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "上傳失敗");
    state.fronts = null;
    state.showFronts = false;
    $("chkFronts").checked = false;
    await fetchSST();
    await loadFileList();
  } catch (e) {
    alert("上傳失敗：" + e.message);
  } finally {
    hideLoading();
    setConnBusy(false);
  }
}

// ── Fishing-ground prediction (ECDF-HSI) ────────────────────
async function loadHabitatParams() {
  try { state.hsiParams = await api("/api/habitat/params"); }
  catch (_) { /* ignore */ }
}

function renderHsiLegend() {
  const box = $("hsiLegend");
  if (!box) return;
  if (!state.hsiParams || !state.hsiParams.prob_levels) {
    box.classList.add("hidden"); return;
  }
  box.classList.remove("hidden");
  box.innerHTML = state.hsiParams.prob_levels.map(l =>
    `<div class="hsi-row"><span class="hsi-swatch" style="background:${l.color}"></span>` +
    `${l.label}（HSI ≥ ${l.min.toFixed(2)}）</div>`
  ).join("");
}

function renderHsiRanges(species) {
  const box = $("hsiRanges");
  if (!box || !state.hsiParams || !state.hsiParams.species[species]) {
    if (box) box.innerHTML = ""; return;
  }
  const sp = state.hsiParams.species[species];
  const r = sp.ranges;
  let html =
    `<span class="hsi-sp"><b>${sp.name_zh}　${sp.name_en}</b>（n=${sp.n} 筆漁獲）最適環境範圍：</span>` +
    `SST ${r.SST.optimal[0]}–${r.SST.optimal[1]} °C<br>` +
    `Chl-a ${r.Chla.optimal[0]}–${r.Chla.optimal[1]} mg/m³<br>` +
    `SSHA ${r.SSHA.optimal[0]}–${r.SSHA.optimal[1]} cm`;
  const s = state.sst && state.sst.kind === "hsi" ? state.sst.sources : null;
  if (s) {
    html += `<br><span class="hsi-src">當日環境資料（Copernicus）：` +
            `SST ${s.sst} · Chl-a ${s.chl} · SSHA ${s.ssha}</span>`;
  }
  box.innerHTML = html;
}

async function predictHabitat(species) {
  const dateEl = $("hsiDate");
  const date = dateEl && dateEl.value;
  if (!date) { alert("請先選擇預測日期"); return; }
  const btns = [$("btnPredictSkj"), $("btnPredictYft")];
  const zh = species === "skipjack" ? "正鰹" : "黃鰭鮪";
  btns.forEach(b => { if (b) b.disabled = true; });
  $("hsiProgressWrapper").classList.remove("hidden");
  showLoading(`${zh}漁場預測中…（擷取 SST／Chl-a／SSHA 環境資料）`);
  setConnBusy(true, "漁場預測中");
  try {
    const d = await api("/api/habitat/predict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ species, date }),
    });
    if (!d.ok) throw new Error(d.error || "預測失敗");
    // Clear overlays that don't apply to a prediction view.
    state.fronts = null; state.showFronts = false;
    if ($("chkFronts")) $("chkFronts").checked = false;
    state.chlOverlay = null; state.currents = null; state.profileLine = null;
    state.sst = {
      kind: "hsi", lon: d.lon, lat: d.lat, values: d.hsi,
      sstGrid: d.sst, chlGrid: d.chl, sshaGrid: d.ssha,
      stats: d.stats, date: d.date, name_zh: d.name_zh,
      sources: d.sources, ranges: d.ranges,
      shape: [d.lat.length, d.lon.length], factor: 1,
    };
    $("hdrDate").textContent = d.date;
    renderHsiLegend();
    renderHsiRanges(species);
    updateStats();
    await drawPlot(false);
    const s = d.sources;
    setStatus(`${d.name_zh}漁場預測 ${d.date}｜最適(≥0.75)格點 ${d.stats.hot_cells} 個` +
      `｜SST ${s.sst} · Chl ${s.chl} · SSHA ${s.ssha}`);
  } catch (e) {
    alert("漁場預測失敗：" + e.message);
  } finally {
    btns.forEach(b => { if (b) b.disabled = false; });
    $("hsiProgressWrapper").classList.add("hidden");
    hideLoading(); setConnBusy(false);
  }
}

// ── Download latest VIIRS (Chl-a) / SSHA and display as main field ──
async function downloadEnv(dataset, label) {
  const dEl = $("dmDate");
  const date = dEl && dEl.value ? dEl.value : null;
  showLoading(`下載 ${label}${date ? " " + date : ""} …（Copernicus Marine 伺服器端裁切）`);
  setConnBusy(true, "下載中");
  try {
    const d = await api("/api/download/env", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(date ? { dataset, date } : { dataset }),
    });
    if (!d.ok) throw new Error(d.error || "下載失敗");
    // 切換主圖層前先清除不適用的疊圖
    state.fronts = null; state.showFronts = false;
    if ($("chkFronts")) $("chkFronts").checked = false;
    state.chlOverlay = null; state.currents = null; state.profileLine = null;
    state.sst = {
      kind: d.kind, lon: d.lon, lat: d.lat, values: d.values,
      stats: d.stats, date: d.date, dataset: d.dataset,
      factor: 1, shape: [d.lat.length, d.lon.length],
    };
    $("hdrDate").textContent = d.date;
    updateStats();
    await drawPlot(false);
    setStatus(`${label} ${d.date}｜範圍 ${d.stats.min}–${d.stats.max}`);
  } catch (e) {
    alert(`${label} 下載失敗：` + e.message);
  } finally {
    hideLoading(); setConnBusy(false);
  }
}

// OSTIA：下載原始（K）或換算攝氏（°C），顯示於右側地圖框
async function ostiaAction(url, label) {
  const dEl = $("dmDate");
  const date = dEl && dEl.value ? dEl.value : null;
  showLoading(`${label}${date ? " " + date : ""} …`);
  setConnBusy(true, "處理中");
  try {
    const d = await api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(date ? { date } : {}),
    });
    if (!d.ok) throw new Error(d.error || "失敗");
    state.fronts = null; state.showFronts = false;
    if ($("chkFronts")) $("chkFronts").checked = false;
    state.chlOverlay = null; state.currents = null; state.profileLine = null;
    state.sst = {
      kind: "sst", unit: d.unit, lon: d.lon, lat: d.lat, values: d.values,
      stats: d.stats, date: d.date, dataset: "mur",
      factor: 1, shape: [d.lat.length, d.lon.length],
    };
    $("hdrDate").textContent = d.date;
    updateStats();
    await drawPlot(false);
    setStatus(`${label} ${d.date}｜範圍 ${d.stats.min}–${d.stats.max} ${d.unit || ""}`);
  } catch (e) {
    alert(`${label}失敗：` + e.message);
  } finally {
    hideLoading(); setConnBusy(false);
  }
}

// ── Wire up controls ────────────────────────────────────────
function wireUp() {
  $("btnDownload").onclick      = () => ostiaAction("/api/ostia/download", "下載 SST（OSTIA）原始");
  if ($("btnOstiaCelsius")) $("btnOstiaCelsius").onclick = () => ostiaAction("/api/ostia/celsius", "OSTIA 換算攝氏並展示");
  $("btnDetectFronts").onclick  = startFrontDetection;
  $("btnReset").onclick         = () => Plotly.relayout(plotDiv, {
    "xaxis.range": [LON_MIN, LON_MAX], "yaxis.range": [LAT_MIN, LAT_MAX],
  });
  $("btnDownloadPNG").onclick   = () => Plotly.downloadImage(plotDiv, plotConfig.toImageButtonOptions);

  $("chkIsotherm").onchange = (e) => { state.showIsotherm = e.target.checked; drawPlot(true); };
  $("chkCoastline").onchange = (e) => { state.showCoastline = e.target.checked; drawPlot(true); };
  $("chkFronts").onchange = (e) => { state.showFronts = e.target.checked; drawPlot(true); };
  $("isoInterval").onchange = (e) => {
    state.isoInterval = parseFloat(e.target.value) || 2.0;
    if (state.showIsotherm) drawPlot(true);
  };
  $("resolutionSelect").onchange = async (e) => {
    state.resolution = parseInt(e.target.value, 10);
    if (state.sst) await fetchSST();
  };

  if ($("fileInput")) $("fileInput").onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) uploadFile(f);
    e.target.value = "";
  };

  if ($("btnPredictSkj")) $("btnPredictSkj").onclick = () => predictHabitat("skipjack");
  if ($("btnPredictYft")) $("btnPredictYft").onclick = () => predictHabitat("yellowfin");

  if ($("btnDownloadModis")) $("btnDownloadModis").onclick = () => downloadEnv("chl", "Chl-a（GlobColour）");
  if ($("btnDownloadSsha")) $("btnDownloadSsha").onclick = () => downloadEnv("ssh", "SSHA（SLA）");

  // Copernicus Marine 登入
  if ($("btnCopLogin")) $("btnCopLogin").onclick = copernicusLogin;
  if ($("btnCopLogout")) $("btnCopLogout").onclick = copernicusLogout;
  if ($("copPass")) $("copPass").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); copernicusLogin(); }
  });
}

// ── Copernicus Marine login ─────────────────────────────────
async function refreshCopernicusStatus(check = false) {
  const dot = $("copDot"), txt = $("copStatusText"), box = $("copLoginBox");
  const logoutBtn = $("btnCopLogout");
  if (!dot || !txt) return;
  try {
    const st = await api(`/api/copernicus/status${check ? "?check=1" : ""}`);
    if (st.logged_in) {
      dot.className = "cop-dot ok";
      let s = `已登入：${st.username || "(帳號)"}`;
      if (st.source === "env") s += "（環境變數）";
      if (st.valid === true) s += " ✓憑證有效";
      else if (st.valid === false) s += " ⚠憑證失效，請重新登入";
      txt.textContent = s;
      if (logoutBtn) logoutBtn.style.display = "";
      if (box) box.open = (st.valid === false);
    } else {
      dot.className = "cop-dot off";
      txt.textContent = (st.toolbox_installed === false)
        ? "未安裝 copernicusmarine 套件（pip install copernicusmarine）"
        : "尚未登入 Copernicus Marine";
      if (logoutBtn) logoutBtn.style.display = "none";
      if (box) box.open = true;
    }
  } catch (e) {
    dot.className = "cop-dot off";
    txt.textContent = "無法取得登入狀態";
  }
}

async function copernicusLogin() {
  const u = ($("copUser").value || "").trim();
  const p = $("copPass").value || "";
  if (!u || !p) { alert("請輸入 Copernicus Marine 帳號與密碼"); return; }
  const btn = $("btnCopLogin");
  btn.disabled = true;
  setConnBusy(true, "登入 Copernicus…");
  try {
    const res = await api("/api/copernicus/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    $("copPass").value = "";                 // 不在前端保留密碼
    await refreshCopernicusStatus(true);
    setConnBusy(false, "已登入 Copernicus");
    if (res && res.message) alert(res.message);
  } catch (e) {
    setConnBusy(false);
    await refreshCopernicusStatus();
    alert("登入失敗：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function copernicusLogout() {
  if (!confirm("確定要登出並移除本機儲存的 Copernicus Marine 憑證？")) return;
  try { await api("/api/copernicus/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  await refreshCopernicusStatus();
}

// ── Init ────────────────────────────────────────────────────
async function init() {
  wireUp();
  // Default the prediction & data-management dates to today.
  const hd = $("hsiDate");
  if (hd && !hd.value) hd.value = new Date().toISOString().slice(0, 10);
  const dm = $("dmDate");
  if (dm && !dm.value) dm.value = new Date().toISOString().slice(0, 10);
  await loadHabitatParams();
  renderHsiLegend();
  await fetchCoastline();
  await fetchStatus();
  await loadFileList();
  refreshCopernicusStatus(true);   // Copernicus 登入狀態（含線上驗證）
}

init();

// ════════════════════════════════════════════════════════════
//  Time-series animation  (ERDDAP jplMURSST41)
// ════════════════════════════════════════════════════════════
function seriesUIReady() {
  $("seriesControls").classList.remove("hidden");
  const n = state.seriesMeta.dates.length;
  const slider = $("seriesSlider");
  slider.max = n - 1;
  slider.value = n - 1;
  state.seriesIndex = n - 1;
}

async function seriesShowFrame(i) {
  if (!state.seriesMeta) return;
  const anom = $("chkAnomaly").checked ? 1 : 0;
  const base = parseInt($("anomalyBaseline").value, 10) || 30;
  const key = `${i}-${anom}-${base}`;
  let frame = state.seriesCache.get(key);
  if (!frame) {
    try {
      frame = await api(`/api/series/frame?index=${i}&anomaly=${anom}&baseline=${base}`);
      if (state.seriesCache.size > 120) state.seriesCache.clear();
      state.seriesCache.set(key, frame);
    } catch (e) {
      appendLocalLog(`❌ 讀取影格失敗：${e.message}`);
      return;
    }
  }
  state.seriesIndex = i;
  state.sst = { ...frame, filename: `series_${frame.date}`, factor: 1,
                shape: [frame.lat.length, frame.lon.length] };
  $("seriesDateLabel").textContent =
    `${frame.date}（${i + 1}/${state.seriesMeta.dates.length}）${frame.anomaly ? " 距平" : ""}`;
  $("hdrDate").textContent = frame.date;
  updateStats();
  await drawPlot(true);
  // 疊圖跟隨影格日期（播放中不同步，避免連續請求）
  if (!state.seriesPlaying) {
    if (state.currents && (state.currents.requested || state.currents.date) !== frame.date) {
      try {
        state.currents = await api(`/api/overlay/currents?date=${frame.date}&max_arrows=${state.vecDensity}`);
        await drawPlot(true);
      } catch (_) { /* 該日尚未發布屬正常 */ }
    }
    if (state.chlOverlay && (state.chlOverlay.requested || state.chlOverlay.date) !== frame.date) {
      try {
        state.chlOverlay = await api(`/api/overlay/chl?date=${frame.date}`);
        await drawPlot(true);
      } catch (_) { /* ignore */ }
    }
  }
}

async function seriesLoad() {
  const start = $("tsStart").value, end = $("tsEnd").value;
  if (!start || !end) { alert("請選擇起迄日期"); return; }
  $("btnSeriesLoad").disabled = true;
  $("tsProgressWrapper").classList.remove("hidden");
  setConnBusy(true, "序列下載中");
  try {
    await api("/api/series/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start, end,
        stride: parseInt($("tsStride").value, 10),
        dataset: $("tsDataset").value || "mur",
      }),
    });
  } catch (e) {
    alert("啟動失敗：" + e.message);
    $("btnSeriesLoad").disabled = false;
    setConnBusy(false);
    return;
  }
  if (state.seriesPolling) clearInterval(state.seriesPolling);
  state.seriesPolling = setInterval(async () => {
    try {
      const st = await api("/api/series/status");
      $("tsProgressFill").style.width = st.progress + "%";
      $("tsProgressLabel").textContent = `${st.progress.toFixed(0)}% ${st.message || ""}`;
      if (!st.active) {
        clearInterval(state.seriesPolling);
        state.seriesPolling = null;
        $("btnSeriesLoad").disabled = false;
        setConnBusy(false);
        setTimeout(() => $("tsProgressWrapper").classList.add("hidden"), 1500);
        if (st.error) { alert("序列下載失敗：" + st.error); return; }
        state.seriesCache.clear();
        state.seriesMeta = await api("/api/series/meta");
        seriesUIReady();
        await seriesShowFrame(state.seriesIndex);
      }
    } catch (_) { /* ignore */ }
  }, 900);
}

function seriesTogglePlay() {
  if (!state.seriesMeta) return;
  state.seriesPlaying = !state.seriesPlaying;
  $("btnSeriesPlay").textContent = state.seriesPlaying ? "⏸ 暫停" : "▶ 播放";
  if (state.seriesTimer) { clearInterval(state.seriesTimer); state.seriesTimer = null; }
  if (state.seriesPlaying) {
    state.seriesTimer = setInterval(async () => {
      const n = state.seriesMeta.dates.length;
      const next = (state.seriesIndex + 1) % n;
      $("seriesSlider").value = next;
      await seriesShowFrame(next);
    }, 700);
  }
}

async function seriesExportGif() {
  if (!state.seriesMeta) { alert("請先下載時間序列"); return; }
  const anom = $("chkAnomaly").checked ? 1 : 0;
  const base = parseInt($("anomalyBaseline").value, 10) || 30;
  const fps = parseInt($("gifFps").value, 10) || 4;
  showLoading("產生 GIF 中（每影格約 1 秒）…");
  try {
    const r = await fetch(`/api/series/export.gif?anomaly=${anom}&baseline=${base}&fps=${fps}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || r.statusText);
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `MUR_${anom ? "anomaly" : "sst"}_${state.seriesMeta.dates[0]}_${state.seriesMeta.dates.at(-1)}.gif`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    alert("GIF 匯出失敗：" + e.message);
  } finally { hideLoading(); }
}

// ════════════════════════════════════════════════════════════
//  Fixed-point station monitoring
// ════════════════════════════════════════════════════════════
async function loadStations(refreshAlerts = true) {
  try {
    const data = await api("/api/stations");
    state.stations = data.stations || [];
  } catch (_) { state.stations = []; }
  renderStationList();
  drawPlot(true);
  if (refreshAlerts) {
    state.stations.forEach(st => refreshStationBadge(st.id));
  }
}

function renderStationList() {
  const box = $("stationList");
  if (!state.stations.length) {
    box.innerHTML = '<div class="muted">— 尚無測站 —</div>';
    return;
  }
  box.innerHTML = state.stations.map(st => `
    <div class="file-item" data-id="${st.id}">
      <div class="fname">⭐ ${st.name}
        <span class="station-badge mono" id="badge-${st.id}">…</span></div>
      <div class="fmeta">${st.lat}°N, ${st.lon}°E
        ${st.t_low != null ? `｜低 ${st.t_low}°` : ""}${st.t_high != null ? `｜高 ${st.t_high}°` : ""}
        <a href="#" class="st-view" data-id="${st.id}">📈 序列</a>
        <a href="#" class="st-del" data-id="${st.id}">🗑</a>
      </div>
    </div>`).join("");
  box.querySelectorAll(".st-view").forEach(el => {
    el.onclick = (ev) => { ev.preventDefault(); showStationSeries(el.dataset.id); };
  });
  box.querySelectorAll(".st-del").forEach(el => {
    el.onclick = async (ev) => {
      ev.preventDefault();
      if (!confirm("確定刪除此測站？")) return;
      await api(`/api/stations/${el.dataset.id}`, { method: "DELETE" });
      await loadStations(false);
    };
  });
}

async function refreshStationBadge(sid) {
  const el = document.getElementById(`badge-${sid}`);
  if (!el) return;
  try {
    const d = await api(`/api/stations/${sid}/series?days=10`);
    if (d.latest_value == null) { el.textContent = "無資料"; return; }
    el.textContent = `${d.latest_value}°C`;
    if (d.alerts && d.alerts.length) {
      el.classList.add("alert");
      el.title = d.alerts.join("\n");
      appendLocalLog(`🚨 ${d.station.name}：${d.alerts.join("；")}`);
    }
  } catch (_) { el.textContent = "—"; }
}

async function showStationSeries(sid) {
  showLoading("查詢 ERDDAP 點位序列…");
  try {
    const d = await api(`/api/stations/${sid}/series?days=90`);
    $("stationModalTitle").textContent =
      `📈 ${d.station.name}（${d.station.lat}°N, ${d.station.lon}°E）近 90 日 SST`;
    const traces = [{
      type: "scatter", mode: "lines+markers",
      x: d.dates, y: d.values,
      line: { color: "#14b8a6", width: 2 },
      marker: { size: 4 },
      hovertemplate: "%{x}<br><b>%{y:.2f} °C</b><extra></extra>",
    }];
    const shapes = [];
    if (d.station.t_high != null) shapes.push({
      type: "line", xref: "paper", x0: 0, x1: 1,
      y0: d.station.t_high, y1: d.station.t_high,
      line: { color: "#ef4444", dash: "dash", width: 1.5 } });
    if (d.station.t_low != null) shapes.push({
      type: "line", xref: "paper", x0: 0, x1: 1,
      y0: d.station.t_low, y1: d.station.t_low,
      line: { color: "#3b82f6", dash: "dash", width: 1.5 } });
    Plotly.newPlot($("stationChart"), traces, {
      paper_bgcolor: "#0b1a30", plot_bgcolor: "#07172e",
      margin: { t: 16, b: 42, l: 48, r: 16 },
      xaxis: { tickfont: { color: "#cbd5e1", size: 10 }, gridcolor: "rgba(42,84,138,.4)" },
      yaxis: { title: { text: "SST (°C)", font: { color: "#cbd5e1", size: 11 } },
               tickfont: { color: "#cbd5e1", size: 10 }, gridcolor: "rgba(42,84,138,.4)" },
      shapes,
    }, { displaylogo: false, responsive: true });
    $("stationAlertBox").textContent = d.alerts && d.alerts.length
      ? "🚨 " + d.alerts.join("；")
      : `最新：${d.latest_date ?? "—"} ${d.latest_value ?? "—"}°C（無警報）`;
    $("stationModal").classList.remove("hidden");
  } catch (e) {
    alert("查詢失敗：" + e.message);
  } finally { hideLoading(); }
}

async function addStationFromForm() {
  const lat = parseFloat($("stLat").value), lon = parseFloat($("stLon").value);
  if (isNaN(lat) || isNaN(lon)) { alert("請輸入座標（或勾選「點擊地圖新增」後點圖）"); return; }
  const body = {
    name: $("stName").value,
    lat, lon,
    t_high: $("stHigh").value === "" ? null : parseFloat($("stHigh").value),
    t_low:  $("stLow").value  === "" ? null : parseFloat($("stLow").value),
  };
  try {
    await api("/api/stations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    ["stName", "stLat", "stLon", "stHigh", "stLow"].forEach(id => $(id).value = "");
    await loadStations();
  } catch (e) { alert("新增失敗：" + e.message); }
}

// ── Wiring for new features ─────────────────────────────────
function wireSeriesAndStations() {
  // sensible default date range: last 14 days (MUR has ~1-2 day latency)
  const today = new Date();
  const end = new Date(today); end.setDate(end.getDate() - 2);
  const start = new Date(end); start.setDate(start.getDate() - 13);
  $("tsEnd").value = end.toISOString().slice(0, 10);
  $("tsStart").value = start.toISOString().slice(0, 10);

  $("btnSeriesLoad").onclick = seriesLoad;
  $("btnSeriesPlay").onclick = seriesTogglePlay;
  $("btnExportGif").onclick = seriesExportGif;
  $("seriesSlider").oninput = (e) => seriesShowFrame(parseInt(e.target.value, 10));
  $("chkAnomaly").onchange = () => seriesShowFrame(state.seriesIndex);
  $("anomalyBaseline").onchange = () => {
    if ($("chkAnomaly").checked) seriesShowFrame(state.seriesIndex);
  };
  // 定點監測功能已移除
}

wireSeriesAndStations();

// ════════════════════════════════════════════════════════════
//  Analysis tools: datasets / transect / chl overlay / export
// ════════════════════════════════════════════════════════════
async function loadDatasetList() {
  try {
    const d = await api("/api/datasets");
    state.datasets = d.datasets || [];
    const sel = $("tsDataset");
    sel.innerHTML = state.datasets.map(x =>
      `<option value="${x.key}">${x.name}</option>`).join("");
  } catch (_) { /* ignore */ }
}

function currentSource() {
  // 目前顯示的是序列影格還是主資料
  if (state.sst && state.sst.nframes !== undefined) {
    return { source: "series", index: state.seriesIndex };
  }
  return { source: "field", index: 0 };
}

// ── Transect ────────────────────────────────────────────────
function handleProfileClick(x, y) {
  state.profilePts.push([x, y]);
  if (state.profilePts.length === 1) {
    $("profileHint").textContent =
      `第一點 ${x.toFixed(2)}°E, ${y.toFixed(2)}°N — 請點第二個端點`;
    state.profileLine = { lons: [x], lats: [y] };
    drawPlot(true);
    return;
  }
  const [p1, p2] = state.profilePts;
  state.profilePts = [];
  state.profileLine = { lons: [p1[0], p2[0]], lats: [p1[1], p2[1]] };
  $("profileHint").textContent = "計算剖面中…";
  drawPlot(true);
  runTransect(p1, p2);
}

async function runTransect(p1, p2) {
  try {
    const body = {
      lon1: p1[0], lat1: p1[1], lon2: p2[0], lat2: p2[1],
      n: 300, ...currentSource(),
    };
    const d = await api("/api/transect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showProfileModal(d);
    $("profileHint").textContent = "請在地圖上點第一個端點…";
  } catch (e) {
    alert("剖面計算失敗：" + e.message);
    $("profileHint").textContent = "請在地圖上點第一個端點…";
  }
}

function showProfileModal(d) {
  $("profileModalTitle").textContent =
    `📏 SST 剖面（${d.label}）— 全長 ${d.total_km} km`;
  const traces = [
    {
      type: "scatter", mode: "lines",
      x: d.dist_km, y: d.values,
      name: "SST",
      line: { color: "#14b8a6", width: 2.2 },
      hovertemplate: "%{x:.1f} km<br><b>%{y:.2f} °C</b><extra></extra>",
    },
    {
      type: "scatter", mode: "lines",
      x: d.dist_km, y: d.grad.map(g => g == null ? null : Math.abs(g)),
      name: "|梯度|",
      yaxis: "y2",
      line: { color: "#f59e0b", width: 1.6, dash: "dot" },
      fill: "tozeroy", fillcolor: "rgba(245,158,11,0.12)",
      hovertemplate: "%{x:.1f} km<br>|∇SST| %{y:.4f} °C/km<extra></extra>",
    },
  ];
  Plotly.newPlot($("profileChart"), traces, {
    paper_bgcolor: "#0b1a30", plot_bgcolor: "#07172e",
    margin: { t: 18, b: 44, l: 52, r: 52 },
    xaxis: {
      title: { text: "沿線距離 (km)", font: { color: "#cbd5e1", size: 11 } },
      tickfont: { color: "#cbd5e1", size: 10 }, gridcolor: "rgba(42,84,138,.4)",
    },
    yaxis: {
      title: { text: "SST (°C)", font: { color: "#14b8a6", size: 11 } },
      tickfont: { color: "#cbd5e1", size: 10 }, gridcolor: "rgba(42,84,138,.4)",
    },
    yaxis2: {
      title: { text: "|∇SST| (°C/km)", font: { color: "#f59e0b", size: 11 } },
      tickfont: { color: "#cbd5e1", size: 10 },
      overlaying: "y", side: "right", rangemode: "tozero",
    },
    legend: { font: { color: "#cbd5e1", size: 10 }, orientation: "h", y: 1.08 },
    hovermode: "x unified",
  }, { displaylogo: false, responsive: true });

  $("profileStats").innerHTML = d.max_grad == null
    ? "沿線無有效資料"
    : `鋒面強度（最大梯度）<b style="color:#f59e0b">${d.max_grad} °C/km</b>
       ＠ ${d.max_grad_at.km} km（${d.max_grad_at.lon}°E, ${d.max_grad_at.lat}°N）
       ${d.max_grad >= 0.05 ? "　🌊 達顯著鋒面標準 (≥0.05 °C/km)" : ""}`;
  $("profileModal").classList.remove("hidden");
}

// ── Chlorophyll overlay ─────────────────────────────────────
async function toggleChlOverlay(on) {
  if (!on) {
    state.chlOverlay = null;
    drawPlot(true);
    return;
  }
  if (!state.sst) { alert("請先載入 SST 資料"); $("chkChlOverlay").checked = false; return; }
  const date = state.sst.date;
  showLoading(`取得 ${date} 葉綠素資料…`);
  try {
    const d = await api(`/api/overlay/chl?date=${date}`);
    state.chlOverlay = d;
    appendLocalLog(`🟢 水色（MODIS）疊圖：${d.date} · ${d.count} 點` +
      `${d.date !== date ? `（${date} 尚未發布，改用最新可用）` : ""}`);
    await drawPlot(true);
  } catch (e) {
    alert("葉綠素取得失敗：" + e.message);
    $("chkChlOverlay").checked = false;
  } finally { hideLoading(); }
}

// ── Export ──────────────────────────────────────────────────
async function exportCurrentView() {
  if (!state.sst) { alert("請先載入資料"); return; }
  const fmt = $("exportFmt").value;
  const xr = plotDiv.layout?.xaxis?.range || [LON_MIN, LON_MAX];
  const yr = plotDiv.layout?.yaxis?.range || [LAT_MIN, LAT_MAX];
  const src = currentSource();
  const q = `fmt=${fmt}&lon0=${xr[0].toFixed(3)}&lon1=${xr[1].toFixed(3)}` +
            `&lat0=${yr[0].toFixed(3)}&lat1=${yr[1].toFixed(3)}` +
            `&source=${src.source}&index=${src.index}`;
  showLoading(`匯出 ${fmt.toUpperCase()} 中…`);
  try {
    const r = await fetch(`/api/export?${q}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || r.statusText);
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename=([^;]+)/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : `export.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    alert("匯出失敗：" + e.message);
  } finally { hideLoading(); }
}

async function downloadFrontsGeojson() {
  showLoading("向量化前緣中…");
  try {
    const r = await fetch("/api/fronts/geojson");
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || r.statusText);
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `fronts_${state.sst?.date || "export"}.geojson`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    alert("GeoJSON 匯出失敗：" + e.message + "（請先執行前緣偵測）");
  } finally { hideLoading(); }
}

// ── Wiring ──────────────────────────────────────────────────
function wireAnalysisTools() {
  $("chkProfile").onchange = (e) => {
    state.profileMode = e.target.checked;
    state.profilePts = [];
    if (!state.profileMode) {
      state.profileLine = null;
      $("profileHint").style.display = "none";
      drawPlot(true);
    } else {
      $("profileHint").style.display = "block";
      $("profileHint").textContent = "請在地圖上點第一個端點…";
    }
  };
  $("btnCloseProfileModal").onclick = () => $("profileModal").classList.add("hidden");
  $("chkChlOverlay").onchange = (e) => toggleChlOverlay(e.target.checked);
  $("chlScale").onchange = (e) => {
    state.chlScale = parseFloat(e.target.value) || 1.0;
    if (state.chlOverlay) drawPlot(true);
  };
  $("btnExport").onclick = exportCurrentView;
  $("btnFrontsGeojson").onclick = downloadFrontsGeojson;
  loadDatasetList();
}

wireAnalysisTools();


// ── Surface current vectors overlay ─────────────────────────
async function toggleCurrentsOverlay(on) {
  if (!on) {
    state.currents = null;
    drawPlot(true);
    return;
  }
  if (!state.sst) { alert("請先載入資料"); $("chkCurrents").checked = false; return; }
  const date = state.sst.date;
  showLoading(`取得 ${date} 海流資料…`);
  try {
    const d = await api(`/api/overlay/currents?date=${date}&max_arrows=${state.vecDensity}`);
    state.currents = d;
    if (d.date !== date) {
      appendLocalLog(`ℹ️ ${date} 海流尚未發布，改用最新可用 ${d.date}`);
    }
    appendLocalLog(`🌀 海流向量：${d.date}（${d.lons.length} 支，最大 ${d.max_speed} m/s）`);
    await drawPlot(true);
  } catch (e) {
    alert("海流資料取得失敗：" + e.message);
    $("chkCurrents").checked = false;
  } finally { hideLoading(); }
}
$("chkCurrents").onchange = (e) => toggleCurrentsOverlay(e.target.checked);
$("vecDensity").onchange = async (e) => {
  state.vecDensity = parseInt(e.target.value, 10) || 3600;
  if (state.currents && state.sst) {
    showLoading("更新箭頭密度…");
    try {
      state.currents = await api(
        `/api/overlay/currents?date=${state.currents.requested || state.currents.date}&max_arrows=${state.vecDensity}`);
      await drawPlot(true);
    } catch (_) { /* ignore */ }
    finally { hideLoading(); }
  }
};
$("vecScale").onchange = (e) => {
  state.vecScale = parseFloat(e.target.value) || 0.55;
  if (state.currents) drawPlot(true);
};


// ════════════════════════════════════════════════════════════
//  ECDF Analysis Modal
//  Shows the CPUE-weighted cumulative distribution + SI curve
//  for each species × environmental variable.
// ════════════════════════════════════════════════════════════

const ecdfState = {
  data: null,          // fetched from /api/habitat/ecdf
  species: "skipjack", // currently active tab
};

// ── Fetch & cache ───────────────────────────────────────────
async function loadEcdfData() {
  if (ecdfState.data) return ecdfState.data;
  try {
    ecdfState.data = await api("/api/habitat/ecdf");
  } catch (e) {
    alert("ECDF 資料載入失敗：" + e.message);
  }
  return ecdfState.data;
}

// ── Draw one variable chart ─────────────────────────────────
function drawEcdfChart(containerId, vd, varLabel, unit, color) {
  if (!vd) return;
  const x = vd.quantiles;   // environmental variable values (x-axis)
  const y = vd.pctls.map(p => p / 100);  // cumulative probability 0–1

  // ── Trapezoidal SI suitability curve (secondary y-axis) ──
  const p05 = vd.p05, p25 = vd.p25, p75 = vd.p75, p95 = vd.p95;
  const siX = [], siY = [];
  x.forEach((xv, i) => {
    let s = 0;
    if (xv >= p25 && xv <= p75) s = 1.0;
    else if (xv >= p05 && xv < p25 && p25 > p05)
      s = (xv - p05) / (p25 - p05);
    else if (xv > p75 && xv <= p95 && p95 > p75)
      s = (p95 - xv) / (p95 - p75);
    siX.push(xv);
    siY.push(Math.max(0, Math.min(1, s)));
  });

  const BG = "#07172e";
  const traces = [
    // Suitable band fill (p10–p90)
    {
      type: "scatter", mode: "none",
      x: [...x.slice(vd.pctls.indexOf(10) < 0 ? 10 : 10,
                     vd.pctls.indexOf(90) < 0 ? 90 : 91),
          ...x.slice(10, 91).slice().reverse()],
      y: [...y.slice(10, 91), ...y.slice(10, 91).slice().reverse()],
      fill: "toself",
      fillcolor: "rgba(56,189,248,0.10)",
      line: { width: 0 },
      hoverinfo: "skip", showlegend: false, name: "適宜帶",
    },
    // Optimal band fill (p25–p75)
    {
      type: "scatter", mode: "none",
      x: [...x.slice(25, 76), ...x.slice(25, 76).slice().reverse()],
      y: [...y.slice(25, 76), ...y.slice(25, 76).slice().reverse()],
      fill: "toself",
      fillcolor: "rgba(251,146,60,0.18)",
      line: { width: 0 },
      hoverinfo: "skip", showlegend: false, name: "最適帶",
    },
    // ECDF cumulative curve
    {
      type: "scatter", mode: "lines",
      x, y,
      line: { color, width: 2.2 },
      name: `ECDF（加權）`,
      hovertemplate: `${varLabel} %{x:.3f} ${unit}<br>累積機率 %{y:.2f}<extra></extra>`,
    },
    // SI trapezoidal curve (secondary y)
    {
      type: "scatter", mode: "lines",
      x: siX, y: siY,
      yaxis: "y2",
      line: { color: "#f59e0b", width: 1.8, dash: "dot" },
      name: "適合度 SI(x)",
      hovertemplate: `${varLabel} %{x:.3f} ${unit}<br>SI = %{y:.2f}<extra></extra>`,
    },
    // Median marker
    {
      type: "scatter", mode: "markers",
      x: [vd.median], y: [0.50],
      marker: { color: "#22c55e", size: 9, symbol: "circle",
                line: { color: "#071520", width: 1.5 } },
      name: `中位數 ${vd.median}`,
      hovertemplate: `中位數 (p50)<br>${varLabel} = %{x:.3f} ${unit}<extra></extra>`,
    },
    // p25 / p75 lines (optimal)
    {
      type: "scatter", mode: "lines",
      x: [vd.p25, vd.p25], y: [0, 1],
      line: { color: "#fb923c", width: 1.2, dash: "dot" },
      hoverinfo: "skip", showlegend: false,
    },
    {
      type: "scatter", mode: "lines",
      x: [vd.p75, vd.p75], y: [0, 1],
      line: { color: "#fb923c", width: 1.2, dash: "dot" },
      hoverinfo: "skip", showlegend: false,
    },
    // p10 / p90 lines (suitable)
    {
      type: "scatter", mode: "lines",
      x: [vd.p10, vd.p10], y: [0, 1],
      line: { color: "#38bdf8", width: 1.0, dash: "longdash" },
      hoverinfo: "skip", showlegend: false,
    },
    {
      type: "scatter", mode: "lines",
      x: [vd.p90, vd.p90], y: [0, 1],
      line: { color: "#38bdf8", width: 1.0, dash: "longdash" },
      hoverinfo: "skip", showlegend: false,
    },
  ];

  const layout = {
    paper_bgcolor: BG, plot_bgcolor: BG,
    margin: { t: 10, b: 48, l: 52, r: 48 },
    xaxis: {
      title: { text: `${varLabel} (${unit})`, font: { color: "#94a3b8", size: 11 } },
      tickfont: { color: "#94a3b8", size: 10 },
      gridcolor: "rgba(42,84,138,0.35)",
      linecolor: "#1f3d65",
    },
    yaxis: {
      title: { text: "累積機率 F(x)", font: { color: color, size: 11 } },
      tickfont: { color: "#94a3b8", size: 10 },
      gridcolor: "rgba(42,84,138,0.35)",
      range: [0, 1.02],
      linecolor: "#1f3d65",
    },
    yaxis2: {
      title: { text: "適合度 SI", font: { color: "#f59e0b", size: 11 } },
      tickfont: { color: "#f59e0b", size: 10 },
      overlaying: "y", side: "right",
      range: [0, 1.02], showgrid: false,
    },
    legend: {
      font: { color: "#cbd5e1", size: 10 }, orientation: "h",
      x: 0, y: -0.22, bgcolor: "rgba(0,0,0,0)",
    },
    hovermode: "x unified",
    // Annotations: p25, p75, p10, p90 labels
    annotations: [
      { x: vd.p25, y: 0.27, text: "p25", showarrow: false,
        font: { color: "#fb923c", size: 9 }, xanchor: "right" },
      { x: vd.p75, y: 0.77, text: "p75", showarrow: false,
        font: { color: "#fb923c", size: 9 }, xanchor: "left" },
      { x: vd.p10, y: 0.10, text: "p10", showarrow: false,
        font: { color: "#38bdf8", size: 9 }, xanchor: "right" },
      { x: vd.p90, y: 0.90, text: "p90", showarrow: false,
        font: { color: "#38bdf8", size: 9 }, xanchor: "left" },
    ],
  };

  const conf = { displaylogo: false, responsive: true,
                 displayModeBar: false };
  Plotly.newPlot($(containerId), traces, layout, conf);
}

// ── Render range summary table ──────────────────────────────
function renderEcdfRangeTable(spData) {
  const box = $("ecdfRangeTable");
  if (!box || !spData) return;

  const VAR_META = {
    SST:  { label: "SST", unit: "°C",     color: "#f87171" },
    Chla: { label: "Chl-a", unit: "mg/m³", color: "#4ade80" },
    SSHA: { label: "SSHA", unit: "cm",     color: "#38bdf8" },
  };

  const rows = Object.entries(VAR_META).map(([key, m]) => {
    const v = spData.vars[key];
    if (!v) return "";
    return `<tr>
      <td>${m.label}<br><small style="color:#64748b">${m.unit}</small></td>
      <td class="suit-cell">${v.p10.toFixed(3)} – ${v.p90.toFixed(3)}</td>
      <td class="opt-cell">${v.p25.toFixed(3)} – ${v.p75.toFixed(3)}</td>
      <td class="med-cell">${v.median.toFixed(3)}</td>
      <td>${v.p05.toFixed(3)}</td>
      <td>${v.p95.toFixed(3)}</td>
    </tr>`;
  }).join("");

  box.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>參數</th>
          <th class="suit-cell">適宜範圍 p10–p90</th>
          <th class="opt-cell">最適範圍 p25–p75</th>
          <th class="med-cell">中位數 p50</th>
          <th>下限 p05</th>
          <th>上限 p95</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="ecdf-si-legend">
      <div class="ecdf-si-badge">
        <span style="background:rgba(251,146,60,0.45);border:1px solid #fb923c"></span>
        最適帶 (p25–p75)
      </div>
      <div class="ecdf-si-badge">
        <span style="background:rgba(56,189,248,0.25);border:1px solid #38bdf8"></span>
        適宜帶 (p10–p90)
      </div>
      <div class="ecdf-si-badge">
        <span style="background:#f59e0b;border:none"></span>
        SI 梯形曲線（虛線，右軸）
      </div>
      <div class="ecdf-si-badge">
        <span style="background:#22c55e;border:none"></span>
        中位數 (p50)
      </div>
    </div>`;
}

// ── Render all three charts for current species ─────────────
function renderEcdfCharts(species) {
  const d = ecdfState.data;
  if (!d || !d.species[species]) return;
  const sp = d.species[species];

  // Update meta bar
  const meta = $("ecdfMeta");
  if (meta) {
    meta.innerHTML =
      `<b>${sp.name_zh}</b> ${sp.name_en}` +
      `<span class="meta-badge">n = ${sp.n.toLocaleString()} 出現點位</span>` +
      `<span class="meta-badge" style="margin-left:6px">方法：CPUE 加權 ECDF</span>` +
      `<span class="meta-badge" style="margin-left:6px">1998–2007 漁獲紀錄</span>`;
  }

  drawEcdfChart("ecdfChartSst",  sp.vars.SST,  "SST",   "°C",     "#f87171");
  drawEcdfChart("ecdfChartChl",  sp.vars.Chla, "Chl-a", "mg/m³",  "#4ade80");
  drawEcdfChart("ecdfChartSsha", sp.vars.SSHA, "SSHA",  "cm",     "#38bdf8");
  renderEcdfRangeTable(sp);
}

// ── Open modal ──────────────────────────────────────────────
async function openEcdfModal(species) {
  ecdfState.species = species || "skipjack";

  // Update tab UI
  document.querySelectorAll(".ecdf-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.sp === ecdfState.species);
  });

  $("ecdfModal").classList.remove("hidden");

  showLoading("載入 ECDF 分析資料…");
  const d = await loadEcdfData();
  hideLoading();
  if (!d) return;

  renderEcdfCharts(ecdfState.species);
}

// ── Wire up ─────────────────────────────────────────────────
function wireEcdfModal() {
  if ($("btnEcdfSkj"))
    $("btnEcdfSkj").onclick = () => openEcdfModal("skipjack");
  if ($("btnEcdfYft"))
    $("btnEcdfYft").onclick = () => openEcdfModal("yellowfin");

  if ($("btnCloseEcdf"))
    $("btnCloseEcdf").onclick = () => $("ecdfModal").classList.add("hidden");

  // Tab switching
  document.querySelectorAll(".ecdf-tab").forEach(tab => {
    tab.onclick = () => {
      ecdfState.species = tab.dataset.sp;
      document.querySelectorAll(".ecdf-tab").forEach(t =>
        t.classList.toggle("active", t.dataset.sp === ecdfState.species));
      if (ecdfState.data) renderEcdfCharts(ecdfState.species);
    };
  });

  // Close on backdrop click
  $("ecdfModal").addEventListener("click", (e) => {
    if (e.target === $("ecdfModal")) $("ecdfModal").classList.add("hidden");
  });
}

wireEcdfModal();

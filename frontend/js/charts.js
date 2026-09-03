/* PR•VISION — charts: velocity line, forecast line, propagation network canvas */
"use strict";

const PRVCharts = (() => {
  Chart.defaults.font.family = "'Inter','Segoe UI',system-ui,sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = "#9ba3ad";
  Chart.defaults.borderColor = "rgba(148,163,184,0.12)";

  const RISK_COLORS = { LOW: "#34d399", MEDIUM: "#fbbf24", HIGH: "#fb923c", CRITICAL: "#f87171" };

  let velocityChart = null;
  let forecastChart = null;

  /* ------------------------------------------------------- velocity chart */
  function renderVelocity(canvas, snapshots, { windowMinutes = 60 } = {}) {
    const labels = snapshots.map((s) =>
      new Date(s.timestamp).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }));
    const shares = snapshots.map((s) => s.shares ?? null);
    const perMin = shares.map((v, i) => {
      if (i === 0) return null;
      const prev = shares[i - 1];
      if (prev === null || v === null) return null;
      const dtMin = (new Date(snapshots[i].timestamp) - new Date(snapshots[i - 1].timestamp)) / 60000;
      return dtMin > 0 ? Math.max(0, (v - prev) / dtMin) : null;
    });

    const gradient = canvas.getContext("2d").createLinearGradient(0, 0, 0, 280);
    gradient.addColorStop(0, "rgba(74, 140, 255, 0.28)");
    gradient.addColorStop(1, "rgba(74, 140, 255, 0)");

    const datasets = [
      {
        label: "Total shares",
        data: shares,
        borderColor: "#4a8cff",
        backgroundColor: gradient,
        fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2, yAxisID: "y",
      },
    ];
    const hasVelocity = perMin.some((v) => v !== null && v > 0);
    if (hasVelocity) {
      datasets.push({
        label: "Shares / minute",
        data: perMin,
        borderColor: "rgba(163, 113, 247, 0.85)",
        backgroundColor: "transparent",
        borderDash: [5, 4], tension: 0.3, pointRadius: 0, borderWidth: 1.6, yAxisID: "y1",
      });
    }

    if (velocityChart) velocityChart.destroy();
    velocityChart = new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { boxWidth: 12, padding: 16, usePointStyle: true } },
          tooltip: { backgroundColor: "#1a1e25", borderColor: "rgba(148,163,184,.25)", borderWidth: 1 },
        },
        scales: {
          y: { beginAtZero: true, title: { display: true, text: "Total shares" }, grid: { color: "rgba(148,163,184,.07)" } },
          ...(hasVelocity ? { y1: { position: "right", beginAtZero: true, title: { display: true, text: "shares/min" }, grid: { drawOnChartArea: false } } } : {}),
          x: { ticks: { maxTicksLimit: 10 }, grid: { display: false } },
        },
        ...{ windowMinutes },
      },
    });
    return velocityChart;
  }

  /* -------------------------------------------------------- forecast chart */
  function renderForecast(canvas, { snapshots, forecast, currentShares }) {
    const labels = snapshots.map((s) =>
      new Date(s.timestamp).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }));
    const history = snapshots.map((s) => s.shares ?? null);

    // forecast series starts at the last historical point (continuity)
    const forecastLabels = [...labels];
    const forecastData = history.map((v) => v);
    const forecastConf = history.map(() => null);
    (forecast || []).forEach((f) => {
      forecastLabels.push(`+${f.horizon_minutes}m`);
      forecastData.push(f.predicted_total_shares);
      forecastConf.push(f.confidence);
    });

    if (forecastChart) forecastChart.destroy();
    forecastChart = new Chart(canvas, {
      type: "line",
      data: {
        labels: forecastLabels,
        datasets: [
          {
            label: "Actual shares",
            data: history,
            borderColor: "#4a8cff",
            backgroundColor: "rgba(74,140,255,.12)",
            fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2,
          },
          {
            label: "Predicted total shares",
            data: forecastData,
            borderColor: "#a371f7",
            backgroundColor: "rgba(163,113,247,.08)",
            borderDash: [7, 5],
            fill: false, tension: 0.3, pointRadius: (ctx) => (ctx.dataIndex >= labels.length ? 4 : 0),
            pointBackgroundColor: "#a371f7",
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { boxWidth: 12, padding: 16, usePointStyle: true } },
          tooltip: {
            backgroundColor: "#1a1e25", borderColor: "rgba(148,163,184,.25)", borderWidth: 1,
            callbacks: {
              afterLabel: (ctx) => {
                if (ctx.dataIndex >= labels.length && forecastConf[ctx.dataIndex] !== null) {
                  return `confidence: ${(forecastConf[ctx.dataIndex] * 100).toFixed(0)}%`;
                }
                return "";
              },
            },
          },
        },
        scales: {
          y: { beginAtZero: true, title: { display: true, text: "Shares" }, grid: { color: "rgba(148,163,184,.07)" } },
          x: { ticks: { maxTicksLimit: 12 }, grid: { display: false } },
        },
      },
    });
    return forecastChart;
  }

  /* ------------------------------------------------- propagation network */
  /**
   * Lightweight deterministic radial cascade layout on canvas.
   * Depth 0 = origin; children fan out in concentric rings by depth.
   */
  function renderNetwork(canvas, { events, isDemo }) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || canvas.parentElement.clientWidth || 400;
    const H = canvas.clientHeight || 300;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    if (!events || !events.length) {
      ctx.fillStyle = "#6b7280";
      ctx.font = "600 12px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No propagation data available from this platform's official API", W / 2, H / 2);
      return;
    }

    // Build adjacency: source → targets (cap for readability)
    const nodes = new Map(); // id → {depth}
    const edges = [];
    nodes.set("ORIGIN", 0);
    const byDepth = { 0: ["ORIGIN"] };
    let maxDepth = 0;
    for (const e of events.slice(0, 220)) {
      const src = e.source_user_id || "ORIGIN";
      const tgt = e.target_user_id || `u${Math.random()}`;
      const srcDepth = nodes.has(src) ? nodes.get(src) : (e.depth ?? 1) - 1;
      const tgtDepth = e.depth ?? srcDepth + 1;
      if (!nodes.has(src)) { nodes.set(src, srcDepth); }
      if (!nodes.has(tgt)) { nodes.set(tgt, tgtDepth); }
      maxDepth = Math.max(maxDepth, tgtDepth);
      edges.push([src, tgt]);
      (byDepth[srcDepth + 1] ||= []).push(tgt);
    }
    maxDepth = Math.min(maxDepth, 4);

    // Layout: origin left-center, depths spread right with vertical jitter (seeded)
    const seed = (str) => { let h = 0; for (const c of str) h = (h * 31 + c.charCodeAt(0)) % 100003; return h / 100003; };
    const positions = new Map();
    positions.set("ORIGIN", { x: 46, y: H / 2 });
    const depthCount = {};
    for (const [id, depth] of nodes) {
      if (id === "ORIGIN" || depth === 0) continue;
      if (depth > maxDepth) continue;
      depthCount[depth] = (depthCount[depth] || 0) + 1;
      const idx = depthCount[depth];
      const gapX = (W - 110) / Math.max(1, maxDepth);
      const x = 46 + gapX * depth;
      const slotCount = Math.max(2, Math.ceil((byDepth[depth] || []).length * 0.9));
      const jitter = (seed(id) - 0.5) * (H - 70) / Math.max(1, Math.min(slotCount, 7));
      const y = H / 2 + jitter;
      positions.set(id, { x, y });
    }

    const NODE_R = { 0: 9, 1: 5, 2: 4, 3: 3.2 };

    // edges first
    ctx.lineWidth = 1;
    for (const [src, tgt] of edges) {
      const a = positions.get(src); const b = positions.get(tgt);
      if (!a || !b) continue;
      const depth = nodes.get(tgt);
      ctx.strokeStyle = depth >= 2 ? "rgba(163,113,247,.28)" : "rgba(74,140,255,.3)";
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    // nodes
    for (const [id, depth] of positions) {
      const p = positions.get(id);
      const r = NODE_R[Math.min(depth, 3)] || 3;
      if (depth === 0) {
        const g = ctx.createRadialGradient(p.x, p.y, 1, p.x, p.y, 18);
        g.addColorStop(0, "rgba(74,140,255,.9)"); g.addColorStop(1, "rgba(74,140,255,0)");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, 18, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "#4a8cff"; ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "#9ba3ad"; ctx.font = "700 10px Inter, sans-serif"; ctx.textAlign = "center";
        ctx.fillText("ORIGIN", p.x, p.y + 26);
      } else {
        ctx.fillStyle = depth === 1 ? "rgba(74,140,255,.75)" : "rgba(163,113,247,.7)";
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
      }
    }
    // labels
    ctx.fillStyle = "#6b7280"; ctx.font = "600 10px Inter, sans-serif"; ctx.textAlign = "left";
    ctx.fillText(`cascade: ${events.length} reshare events · depth ≤ ${maxDepth}`, 12, H - 12);
    if (isDemo) {
      ctx.fillStyle = "rgba(163,113,247,.9)"; ctx.font = "700 10px Inter, sans-serif";
      ctx.fillText("DEMO DATA", W - 84, 18);
    }
  }

  return { RISK_COLORS, renderVelocity, renderForecast, renderNetwork };
})();

/* PR•VISION — dashboard orchestrator: KPIs, auto-refresh, charts, controls */
"use strict";

(() => {
  const state = {
    windowMinutes: 60,
    filters: { platform: "", label: "", search: "" },
    allPlatforms: [],
    refreshSeconds: 8,
    autoRefresh: true,
    timer: null,
    ingestionRunning: false,
  };

  const $ = (id) => document.getElementById(id);

  /* ------------------------------------------------------------- KPIs */
  async function refreshKpis() {
    const summary = await PRVApi.dashboardSummary();
    PRVUtils.countUp($("kpi-posts").querySelector("[data-count]"), summary.posts_monitored);
    PRVUtils.countUp($("kpi-critical").querySelector("[data-count]"), summary.critical_alerts);
    PRVUtils.countUp($("kpi-high").querySelector("[data-count]"), summary.high_risk_posts);
    PRVUtils.countUp($("kpi-forecast").querySelector("[data-count]"), summary.predicted_shares_60m || 0);
    PRVUtils.countUp($("kpi-risk").querySelector("[data-count]"), summary.average_risk ?? 0, { decimals: 2 });
    const platformText = Object.entries(summary.platform_counts)
      .sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k}:${v}`).join(" · ");
    $("kpi-platforms-sub").textContent = platformText || "no posts yet";
    $("kpi-risk-sub").textContent = summary.average_risk !== null
      ? "misinformation component (0–1)"
      : "no scored posts yet";
    return summary;
  }

  /* ---------------------------------------------------------- velocity */
  async function refreshVelocityChart() {
    const select = $("velocity-post");
    const postId = select.value;
    if (!postId) return;
    try {
      const metrics = await PRVApi.postMetrics(postId, { windowMinutes: state.windowMinutes });
      const canvas = $("chart-velocity");
      if (metrics.snapshots.length === 0) {
        drawEmpty(canvas, "No snapshots in this window yet");
        return;
      }
      PRVCharts.renderVelocity(canvas, metrics.snapshots, { windowMinutes: state.windowMinutes });
    } catch (err) {
      console.warn("velocity chart:", err.message);
    }
  }

  function drawEmpty(canvas, message) {
    const ctx = canvas.getContext("2d");
    const { width, height } = canvas;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#5d6b82";
    ctx.font = "600 12px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(message, width / 2, height / 2);
  }

  /* ---------------------------------------------------------- forecast */
  async function refreshForecastChart() {
    const select = $("velocity-post");
    const postId = select.value;
    if (!postId) return;
    try {
      const [metrics, prediction] = await Promise.all([
        PRVApi.postMetrics(postId, { windowMinutes: 120 }),
        PRVApi.postPrediction(postId, false),
      ]);
      const horizons = Object.values(prediction.horizons || {}).map((h) => ({
        horizon_minutes: h.horizon_minutes,
        predicted_total_shares: h.predicted_total_shares,
        predicted_additional_shares: h.predicted_additional_shares,
        confidence: h.confidence,
        prediction_type: h.prediction_type,
      }));
      PRVCharts.renderForecast($("chart-forecast"), {
        snapshots: metrics.snapshots,
        forecast: horizons,
        currentShares: prediction.current_shares,
      });
    } catch (err) {
      console.warn("forecast chart:", err.message);
    }
  }

  /* ----------------------------------------------------------- network */
  async function refreshNetworkChart() {
    const select = $("velocity-post");
    const postId = select.value;
    if (!postId) return;
    try {
      const propagation = await PRVApi.postPropagation(postId);
      PRVCharts.renderNetwork($("chart-network"), propagation);
    } catch (err) {
      console.warn("network chart:", err.message);
    }
  }

  /* ------------------------------------------------------------- cycle */
  async function refreshAll({ silent = true } = {}) {
    try {
      const [health, platforms] = await Promise.all([
        PRVApi.health(),
        PRVApi.platforms().catch(() => ({ platforms: [] })),
      ]);
      PRVAlerts.renderHealth(health, platforms);
      PRVAlerts.renderSources(platforms);
      state.allPlatforms = (platforms.platforms || []).map((p) => p.platform);
      state.ingestionRunning = health.ingestion === "running";
      updateIngestionButton();

      await Promise.all([refreshKpis(), PRVPosts.loadQueue(state.filters)]);
      await Promise.all([refreshVelocityChart(), refreshForecastChart(), refreshNetworkChart()]);
      PRVAlerts.updateLastRefresh();
      if (!silent) PRVAlerts.toast("Dashboard refreshed", "success", 1600);
    } catch (err) {
      if (!silent) PRVAlerts.toast(`Refresh failed: ${err.message}`, "error");
      console.warn("refresh:", err.message);
    }
  }

  function scheduleAutoRefresh() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (state.autoRefresh) refreshAll({ silent: true });
    }, state.refreshSeconds * 1000);
  }

  /* ----------------------------------------------------------- controls */
  function updateIngestionButton() {
    const btn = $("btn-ingestion-toggle");
    btn.textContent = state.ingestionRunning ? "Stop Ingestion" : "Start Ingestion";
    btn.classList.toggle("btn-primary", !state.ingestionRunning);
  }

  function wireEvents() {
    // window segmented control
    document.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.windowMinutes = parseInt(btn.dataset.window, 10);
        refreshVelocityChart();
      });
    });

    $("velocity-post").addEventListener("change", () => {
      refreshVelocityChart();
      refreshForecastChart();
      refreshNetworkChart();
    });

    // filters
    $("filter-platform").addEventListener("change", (e) => {
      state.filters.platform = e.target.value;
      refreshAll({ silent: true });
    });
    $("filter-label").addEventListener("change", (e) => {
      state.filters.label = e.target.value;
      PRVPosts.loadQueue(state.filters).catch(() => {});
    });
    $("filter-search").addEventListener("input", PRVUtils.debounce((e) => {
      state.filters.search = e.target.value.trim();
      PRVPosts.loadQueue(state.filters).catch(() => {});
    }, 400));

    // ingestion toggle
    $("btn-ingestion-toggle").addEventListener("click", async () => {
      const btn = $("btn-ingestion-toggle");
      btn.disabled = true;
      try {
        if (state.ingestionRunning) {
          await PRVApi.stopIngestion();
          PRVAlerts.toast("Ingestion stopped", "warning");
        } else {
          // Restart EVERY configured platform loop (demo + real harvesters),
          // not just demo — stopping ingestion must not silently drop the
          // real-platform pipelines.
          const platforms = (state.allPlatforms && state.allPlatforms.length)
            ? state.allPlatforms : undefined;
          await PRVApi.startIngestion(platforms ? { platforms } : {});
          PRVAlerts.toast("Live ingestion started for all platforms — metrics will update automatically", "success");
        }
        state.ingestionRunning = !state.ingestionRunning;
        updateIngestionButton();
        setTimeout(() => refreshAll({ silent: true }), 1200);
      } catch (err) {
        PRVAlerts.toast(`Ingestion toggle failed: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
      }
    });

    // add demo post
    $("btn-demo-add").addEventListener("click", async () => {
      const btn = $("btn-demo-add");
      btn.disabled = true;
      btn.textContent = "Generating…";
      try {
        const result = await PRVApi.generateDemo({ num_posts: 1 });
        PRVAlerts.toast(`Demo post #${result.posts[0].post_id} created through the full pipeline`, "success");
        await refreshAll({ silent: true });
      } catch (err) {
        PRVAlerts.toast(`Demo generation failed: ${err.message}`, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "+ Demo Post";
      }
    });

    // drawer close
    $("drawer-close").addEventListener("click", PRVPosts.closeDrawer);
    $("drawer-backdrop").addEventListener("click", PRVPosts.closeDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") PRVPosts.closeDrawer();
    });

    // pause auto-refresh when tab hidden
    document.addEventListener("visibilitychange", () => {
      state.autoRefresh = !document.hidden;
    });
  }

  /* --------------------------------------------------------------- boot */
  async function boot() {
    wireEvents();
    scheduleAutoRefresh();

    try {
      const health = await PRVApi.health();
      const summary = await refreshKpis();

      // First run with no data → auto-generate demo posts so the dashboard is never empty
      if (summary.posts_monitored === 0) {
        PRVAlerts.toast("First run detected — generating demo data through the full pipeline…", "info", 5000);
        try {
          await PRVApi.generateDemo({ num_posts: 5 });
          PRVAlerts.toast("Demo data ready — 5 archetypes ingested, scored, and ready to explore", "success", 5000);
        } catch (err) {
          PRVAlerts.toast(`Demo generation failed: ${err.message}`, "error", 6000);
        }
      }

      // Cold start hint
      if (health.forecast_model !== "loaded") {
        PRVAlerts.toast("Models not trained yet — forecasts use the transparent velocity baseline. Train via scripts/train_models.py or POST /api/ml/train.", "warning", 8000);
      }
    } catch (err) {
      PRVAlerts.toast(`Backend unreachable: ${err.message}`, "error", 8000);
    }

    await refreshAll({ silent: true });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

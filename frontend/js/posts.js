/* PR•VISION — posts: priority queue table + investigation drawer */
"use strict";

const PRVPosts = (() => {
  const state = { posts: [], selectedId: null };

  /* ------------------------------------------------------ priority queue */
  async function loadQueue(filters = {}) {
    const [priority, trending] = await Promise.all([
      PRVApi.highPriority({ label: filters.label, platform: filters.platform, limit: 60 }),
      PRVApi.trending({ platform: filters.platform, limit: 60 }),
    ]);

    // Merge: high-priority (scored) + trending posts not yet in the queue.
    // Enrich priority rows with velocity/accel from trending where missing.
    const byId = new Map();
    const trendById = new Map();
    for (const t of trending.posts) trendById.set(t.post_id, t);
    for (const p of priority.posts) {
      byId.set(p.post_id, p);
      const t = trendById.get(p.post_id);
      if (t) {
        if (p.share_velocity === null || p.share_velocity === undefined) p.share_velocity = t.share_velocity;
        if (p.share_acceleration === null || p.share_acceleration === undefined) p.share_acceleration = t.share_acceleration;
      }
    }
    for (const t of trending.posts) {
      if (!byId.has(t.post_id)) {
        byId.set(t.post_id, {
          post_id: t.post_id,
          platform: t.platform,
          external_post_id: t.external_post_id,
          content: t.content,
          is_demo: t.is_demo,
          current_shares: t.current_shares,
          share_velocity: t.share_velocity,
          share_acceleration: t.share_acceleration,
          predicted_additional_shares: t.predicted_additional_shares_60m,
          misinformation_risk: t.misinformation_risk,
          intervention_priority: t.intervention_priority ?? 0,
          priority_label: t.priority_label ?? (t.share_velocity > 5 ? "MEDIUM" : "LOW"),
          top_factors: [],
        });
      }
    }

    let rows = [...byId.values()];
    const search = (filters.search || "").toLowerCase();
    if (search) rows = rows.filter((r) => (r.content || "").toLowerCase().includes(search));
    if (filters.label) rows = rows.filter((r) => r.priority_label === filters.label);
    rows.sort((a, b) => (b.intervention_priority ?? 0) - (a.intervention_priority ?? 0));

    state.posts = rows.slice(0, 25);
    renderQueue();
    populatePostSelectors();
  }

  function renderQueue() {
    const tbody = document.getElementById("priority-tbody");
    tbody.innerHTML = "";
    if (!state.posts.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty-row">No posts match. Generate demo data or adjust filters.</td></tr>`;
      return;
    }
    for (const post of state.posts) {
      const tr = document.createElement("tr");
      tr.dataset.postId = post.post_id;
      tr.addEventListener("click", () => openDrawer(post.post_id));
      tr.innerHTML = `
        <td class="cell-post"><span class="post-content">${PRVUtils.escapeHtml(PRVUtils.truncate(post.content, 84))}</span>${post.is_demo ? '<span class="demo-tag">demo</span>' : ""}</td>
        <td>${PRVUtils.escapeHtml(PRVUtils.platformLabel(post.platform))}</td>
        <td class="num">${PRVUtils.compact(post.current_shares)}</td>
        <td class="num">${post.share_velocity !== null && post.share_velocity !== undefined ? post.share_velocity.toFixed(1) + "/m" : "—"}</td>
        <td class="num">${fmtAccel(post.share_acceleration)}</td>
        <td class="num">+${PRVUtils.compact(post.predicted_additional_shares)}</td>
        <td class="num">${riskCell(post.misinformation_risk)}</td>
        <td class="num cell-priority priority-val-${post.priority_label}">${post.intervention_priority !== null ? post.intervention_priority.toFixed(0) : "—"}</td>
        <td><span class="badge badge-${post.priority_label}">${post.priority_label}</span></td>`;
      tbody.appendChild(tr);
    }
  }

  function fmtAccel(v) {
    if (v === null || v === undefined) return "—";
    const arrow = v > 0.02 ? "▲" : v < -0.02 ? "▼" : "—";
    return `${arrow} ${Math.abs(v).toFixed(2)}`;
  }

  function riskCell(risk) {
    if (risk === null || risk === undefined) return "—";
    const bar = `<span class="risk-bar"><i style="width:${Math.round(risk * 100)}%"></i></span>`;
    return `${bar}${risk.toFixed(2)}`;
  }

  /* ------------------------------------------------- post select options */
  function populatePostSelectors() {
    const select = document.getElementById("velocity-post");
    const previous = select.value;
    select.innerHTML = "";
    for (const post of state.posts.slice(0, 20)) {
      const option = document.createElement("option");
      option.value = post.post_id;
      option.textContent = `#${post.post_id} · ${PRVUtils.truncate(post.content, 42)}`;
      select.appendChild(option);
    }
    if (previous && [...select.options].some((o) => o.value === previous)) select.value = previous;
    else if (state.posts.length) select.value = String(state.posts[0].post_id);
  }

  /* --------------------------------------------------------------- drawer */
  async function openDrawer(postId) {
    state.selectedId = postId;
    const drawer = document.getElementById("post-drawer");
    const backdrop = document.getElementById("drawer-backdrop");
    const body = document.getElementById("drawer-body");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    backdrop.hidden = false;
    body.innerHTML = `<div class="drawer-loading">Loading investigation data…</div>`;

    try {
      const [post, metrics, prediction, propagation] = await Promise.all([
        PRVApi.post(postId),
        PRVApi.postMetrics(postId, { windowMinutes: 120, limit: 400 }),
        PRVApi.postPrediction(postId, true),
        PRVApi.postPropagation(postId),
      ]);
      renderDrawer(post, metrics, prediction, propagation, postId);
      // detail charts
      const vCanvas = document.getElementById("drawer-velocity-canvas");
      if (vCanvas) PRVCharts.renderVelocity(vCanvas, metrics.snapshots, { windowMinutes: 120 });
      const nCanvas = document.getElementById("drawer-network-canvas");
      if (nCanvas) PRVCharts.renderNetwork(nCanvas, propagation);
    } catch (err) {
      body.innerHTML = `<div class="drawer-loading">Failed to load: ${PRVUtils.escapeHtml(err.message)}</div>`;
    }
  }

  function renderDrawer(post, metrics, prediction, propagation, postId) {
    const body = document.getElementById("drawer-body");
    const latest = post.latest_metrics || {};
    const lastSnapshot = metrics.snapshots[metrics.snapshots.length - 1] || {};
    const horizons = prediction.horizons || {};
    const velocity = prediction.share_velocity;
    const labelClass = prediction.priority_label || "LOW";

    const metricCells = [
      ["Likes", lastSnapshot.likes], ["Comments", lastSnapshot.comments],
      ["Shares", lastSnapshot.shares], ["Views", lastSnapshot.views],
      ["Followers", lastSnapshot.followers], ["Unique Sharers", lastSnapshot.unique_sharers],
    ];

    body.innerHTML = `
      <section>
        <h3>Content ${post.is_demo ? '<span class="demo-tag">demo</span>' : ""}</h3>
        <div class="post-content-box">${PRVUtils.escapeHtml(post.content)}</div>
        <p class="drawer-note" style="margin-top:8px">${PRVUtils.escapeHtml(post.platform)} · posted ${PRVUtils.timeAgo(post.posted_at)} · <a href="${PRVUtils.escapeHtml(post.url || "#")}" target="_blank" rel="noopener" style="color:var(--accent)">open original ↗</a></p>
      </section>

      <section>
        <h3>Intervention Priority</h3>
        <div class="priority-hero">
          <div class="score priority-val-${labelClass}" id="drawer-priority-score">${prediction.intervention_priority.toFixed(0)}</div>
          <div class="meta">
            <span class="badge badge-${labelClass}">${prediction.priority_label}</span>
            <div class="sub">spread risk ${(prediction.spread_risk * 100).toFixed(0)}% · misinfo risk ${(prediction.misinformation_risk * 100).toFixed(0)}% (${PRVUtils.escapeHtml(prediction.misinformation_model_layer)})</div>
            <div class="priority-meter"><i class="priority-val-${labelClass}" style="width:${prediction.intervention_priority}%; background:currentColor"></i></div>
          </div>
        </div>
      </section>

      <section>
        <h3>Metrics</h3>
        <div class="metric-grid">
          ${metricCells.map(([k, v]) => `
            <div class="metric-cell"><div class="k">${k}</div><div class="v">${v !== null && v !== undefined ? PRVUtils.withCommas(v) : "unavailable"}</div></div>`).join("")}
        </div>
        <p class="drawer-note" style="margin-top:6px">“unavailable” = the platform's official API does not expose this metric — never fabricated.</p>
      </section>

      <section>
        <h3>Growth Signals</h3>
        <div class="growth-grid">
          <div class="metric-cell"><div class="k">Share Velocity</div><div class="v">${velocity !== null && velocity !== undefined ? velocity.toFixed(2) + " /min" : "—"}</div></div>
          <div class="metric-cell"><div class="k">Share Acceleration</div><div class="v">${fmtAccel(drawerFeature(metrics, prediction, "share_acceleration"))}</div></div>
          <div class="metric-cell"><div class="k">Engagement Velocity</div><div class="v">${fmtAccel(drawerFeature(metrics, prediction, "engagement_velocity"))}</div></div>
          <div class="metric-cell"><div class="k">Unique Sharer Growth</div><div class="v">${fmtAccel(drawerFeature(metrics, prediction, "unique_sharer_growth_rate"))}</div></div>
        </div>
        <div class="chart-box" style="height:190px;margin-top:12px"><canvas id="drawer-velocity-canvas"></canvas></div>
      </section>

      <section>
        <h3>Share Forecast</h3>
        <div class="forecast-grid">
          ${Object.values(horizons).map((h) => `
            <div class="forecast-cell">
              <div class="h">+${h.horizon_minutes}m</div>
              <div class="v">+${PRVUtils.compact(h.predicted_additional_shares)}</div>
              <div class="c">${(h.confidence * 100).toFixed(0)}% · ${h.prediction_type}</div>
            </div>`).join("")}
        </div>
        <p class="drawer-note" style="margin-top:6px">${horizons["60"] && horizons["60"].reason ? "" + PRVUtils.escapeHtml(horizons["60"].reason) + " — transparent baseline used, confidence lowered." : "XGBoost forecast trained on chronological data."}</p>
      </section>

      <section>
        <h3>Misinformation Risk — ${prediction.misinformation_risk_label}</h3>
        <div class="priority-hero" style="padding:12px 16px">
          <div class="score" style="color:${PRVCharts.RISK_COLORS[prediction.misinformation_risk_label] || 'var(--text-1)'}">${prediction.misinformation_risk.toFixed(2)}</div>
          <div class="meta"><div class="sub">Stylistic estimate from content signals (${PRVUtils.escapeHtml(prediction.misinformation_model_layer)}). This is <strong>not</strong> a truth verdict — human review required.</div></div>
        </div>
      </section>

      <section>
        <h3>Why This Score</h3>
        <ul class="explanation-list">
          ${prediction.explanation.map((r) => `<li>${PRVUtils.escapeHtml(r)}</li>`).join("")}
        </ul>
      </section>

      <section>
        <h3>Top Contributing Factors</h3>
        <div class="factors">
          ${prediction.top_factors.map((f) => `<span class="factor-chip">${PRVUtils.escapeHtml(f)}</span>`).join("")}
        </div>
      </section>

      <section>
        <h3>Propagation Network</h3>
        <div class="chart-box network-box" style="height:230px"><canvas id="drawer-network-canvas"></canvas></div>
      </section>
    `;
    document.getElementById("drawer-sub").textContent =
      `Post #${postId} · ${post.platform} · priority computed ${PRVUtils.timeAgo(new Date().toISOString())}`;
  }

  function drawerFeature(metrics, prediction, key) {
    // features endpoint is richer; fall back to prediction payload fields
    return prediction[key] ?? null;
  }

  function closeDrawer() {
    const drawer = document.getElementById("post-drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    document.getElementById("drawer-backdrop").hidden = true;
    state.selectedId = null;
  }

  function selectedId() { return state.selectedId; }

  return { loadQueue, openDrawer, closeDrawer, selectedId, populatePostSelectors };
})();

/* escapeHtml lives on utils (added here to keep utils.js tidy) */
PRVUtils.escapeHtml = PRVUtils.escapeHtml || ((s) => {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
});

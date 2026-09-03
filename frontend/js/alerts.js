/* PR•VISION — alerts: status pills, source list, toasts */
"use strict";

const PRVAlerts = (() => {

  function toast(message, kind = "info", ms = 3800) {
    const stack = document.getElementById("toast-stack");
    const node = document.createElement("div");
    node.className = `toast${kind === "error" ? " toast-error" : kind === "success" ? " toast-success" : kind === "warning" ? " toast-warning" : ""}`;
    node.textContent = message;
    stack.appendChild(node);
    setTimeout(() => {
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 320);
    }, ms);
  }

  function setPill(id, state, value, title) {
    const pill = document.getElementById(id);
    if (!pill) return;
    pill.classList.remove("ok", "warn", "bad");
    pill.classList.add(state);
    pill.querySelector(".value").textContent = value;
    if (title) pill.title = title;
  }

  /** Update header status pills from /api/health + /api/platforms. */
  function renderHealth(health, platforms) {
    // System
    const sysState = health.status === "healthy" ? "ok" : health.status === "degraded" ? "warn" : "bad";
    setPill("pill-system", sysState, health.status.toUpperCase());

    // Database
    const dbState = health.database === "connected" ? "ok" : "bad";
    setPill("pill-db", dbState, health.database.toUpperCase());

    // Pipeline (ingestion scheduler)
    const running = health.ingestion === "running";
    setPill("pill-pipeline", running ? "ok" : "warn", running ? "LIVE" : "IDLE",
      `Ingestion scheduler: ${health.ingestion}`);

    // Model
    const modelState = health.forecast_model === "loaded" ? "ok" : "warn";
    setPill("pill-model", modelState, health.forecast_model === "loaded" ? "XGB READY" : "COLD START");

    // Platform chips
    const chips = document.getElementById("platform-chips");
    if (chips && platforms && platforms.platforms) {
      chips.innerHTML = "";
      for (const p of platforms.platforms) {
        const chip = document.createElement("span");
        chip.className = `chip${p.configured || p.state === "HARVEST" ? "" : " off"}`;
        chip.textContent = PRVUtils.platformLabel(p.platform);
        chip.title = p.detail || (p.configured ? "configured"
          : p.state === "HARVEST" ? "real posts via web-search harvester"
          : "official API credentials not configured");
        chips.appendChild(chip);
      }
    }
  }

  /** Connector source list in the side panel. */
  function renderSources(platforms) {
    const list = document.getElementById("source-list");
    if (!list) return;
    list.innerHTML = "";
    if (!platforms.platforms.length) {
      list.innerHTML = `<li class="placeholder-row">No connectors registered.</li>`;
      return;
    }
    for (const p of platforms.platforms) {
      // Canonical state drives the label; internal status drives the colour.
      const stateLabel = p.state || (p.status === "not_configured" ? "NO CREDS" : p.status.toUpperCase());
      const stateClass =
        (stateLabel === "HARVEST" || stateLabel === "CONNECTED") ? "healthy"
        : stateLabel === "NO CREDS" || stateLabel === "DISABLED" ? "not_configured"
        : stateLabel === "DEGRADED" || stateLabel === "RATE_LIMITED" || stateLabel === "AUTH_REQUIRED" ? "degraded"
        : "error";
      const meta = p.detail
        ? p.detail
        : p.last_successful_fetch
          ? `last fetch ${PRVUtils.timeAgo(p.last_successful_fetch)}`
          : (p.configured ? "awaiting first fetch" : "credentials not configured");
      const li = document.createElement("li");
      li.className = "source-item";
      li.title = meta;
      li.innerHTML = `
        <div>
          <div class="name">${PRVUtils.escapeHtml(PRVUtils.platformLabel(p.platform))}</div>
          <div class="meta">${PRVUtils.escapeHtml(
            meta.length > 64 ? meta.slice(0, 61) + "…" : meta)}</div>
        </div>
        <span class="source-state ${stateClass}">${PRVUtils.escapeHtml(stateLabel)}</span>`;
      list.appendChild(li);
    }
  }

  function updateLastRefresh() {
    const node = document.getElementById("last-update");
    if (node) node.textContent = `updated ${PRVUtils.clockTime()}`;
  }

  /* ------------------------------------------------------- live alert feed */
  const SEV_META = {
    CRITICAL: { cls: "sev-critical", icon: "" },
    HIGH: { cls: "sev-high", icon: "" },
    MEDIUM: { cls: "sev-medium", icon: "" },
    LOW: { cls: "sev-low", icon: "" },
  };
  const KIND_META = {
    misinfo_risk: "Misinformation risk",
    acceleration_spike: "Acceleration spike",
    forecast_jump: "Forecast jump",
    evidence_conflict: "Conflicting evidence",
    media_signal: "Suspicious media",
  };

  async function refreshFeed() {
    const feed = document.getElementById("alert-feed");
    const count = document.getElementById("alert-count");
    if (!feed) return;
    try {
      const [list, summary] = await Promise.all([
        PRVApi.alerts({ limit: 25 }),
        PRVApi.alertSummary(),
      ]);
      if (count) {
        count.textContent = summary.total_unacknowledged ?? 0;
        count.classList.toggle("has-critical", (summary.unacknowledged?.CRITICAL ?? 0) > 0);
      }
      const alerts = list.alerts || [];
      if (!alerts.length) {
        feed.innerHTML = `<div class="muted" style="padding:8px 2px;">No alerts yet — the engine evaluates
          triggers continuously from live scores and verification results.</div>`;
        return;
      }
      feed.innerHTML = alerts.map((a) => {
        const sev = SEV_META[a.severity] || SEV_META.LOW;
        return `
        <article class="alert-row ${sev.cls} ${a.acknowledged ? "acked" : ""}" data-alert="${a.alert_id}">
          <span class="alert-icon" aria-hidden="true">${sev.icon}</span>
          <div class="alert-main">
            <header>
              <span class="alert-sev">${PRVUtils.escapeHtml(a.severity)}</span>
              <span class="alert-kind">${PRVUtils.escapeHtml(KIND_META[a.kind] || a.kind)}</span>
              <span class="alert-time muted">${PRVUtils.escapeHtml((a.created_at || "").slice(5, 16).replace("T", " "))}</span>
            </header>
            <p>${PRVUtils.escapeHtml(a.title)}</p>
            <div class="alert-msg muted">${PRVUtils.escapeHtml(a.message)}</div>
          </div>
          <div class="alert-actions">
            ${a.verification_job_id ? `<a class="btn btn-ghost btn-sm" href="/report/${a.verification_job_id}">Report</a>` : ""}
            ${a.post_id ? `<button class="btn btn-ghost btn-sm" data-open-post="${a.post_id}" type="button">Post</button>` : ""}
            ${!a.acknowledged ? `<button class="btn btn-ghost btn-sm" data-ack="${a.alert_id}" type="button">Ack</button>` : `<span class="muted acked-mark">✓</span>`}
          </div>
        </article>`;
      }).join("");

      feed.querySelectorAll("[data-ack]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await PRVApi.acknowledgeAlert(Number(btn.dataset.ack));
            PRVAlerts.toast("Alert acknowledged", "success");
            refreshFeed();
          } catch (err) {
            PRVAlerts.toast(err.status === 401 ? "Sign in to acknowledge alerts." : err.message, "error");
            btn.disabled = false;
          }
        });
      });
      feed.querySelectorAll("[data-open-post]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const row = btn.closest(".alert-row");
          row?.remove();
          try {
            window.dispatchEvent(new CustomEvent("prv:open-post", { detail: Number(btn.dataset.openPost) }));
          } catch { /* drawer hook may not exist on this page */ }
        });
      });
    } catch (err) {
      feed.innerHTML = `<div class="muted" style="padding:8px 2px;">Alert feed unavailable: ${PRVUtils.escapeHtml(err.message)}</div>`;
    }
  }

  document.getElementById("alert-reload")?.addEventListener("click", refreshFeed);
  setInterval(refreshFeed, 30000);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refreshFeed);
  } else {
    refreshFeed();
  }

  return { toast, renderHealth, renderSources, updateLastRefresh, refreshFeed };
})();

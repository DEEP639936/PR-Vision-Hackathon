/* PR•VISION — Investigation cases workspace (spec #14) */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const esc = (window.PRVUtils && PRVUtils.escapeHtml) || ((s) => String(s ?? ""));

  const state = { authenticated: false, user: null, cases: [], selected: null };

  // ------------------------------------------------------------------ toasts
  function toast(message, kind = "info") {
    const stack = $("#toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 400); }, 4200);
  }

  // ------------------------------------------------------------------ auth chip
  async function loadAuth() {
    let token = null;
    try { token = localStorage.getItem("prv_token"); } catch { }
    if (!token) return;
    try {
      const res = await PRVApi.me();
      state.authenticated = true;
      state.user = res.user;
      const area = $("#auth-area");
      if (area) {
        area.dataset.auth = "signed-in";
        area.innerHTML = `<span class="user-chip" title="${esc(res.user.email)}">
            <span class="dot"></span>${esc(res.user.display_name || res.user.email)} · ${esc(res.user.role)}</span>
            <button class="btn btn-ghost btn-sm" id="logout-btn" type="button">Log out</button>`;
        $("#logout-btn").addEventListener("click", async () => {
          try { await PRVApi.logout(); } catch { /* session may already be dead */ }
          localStorage.removeItem("prv_token");
          localStorage.removeItem("prv_user");
          window.location.assign("/login");
        });
      }
    } catch { /* stale token — leave signed out */ }
  }

  // ------------------------------------------------------------------ list
  async function loadCases() {
    const container = $("#cases-list");
    if (!state.authenticated) {
      container.innerHTML = `<div class="glass panel muted">
        🔒 <strong>Sign in required.</strong> Cases are saved investigations tied to operator accounts.
        <a class="btn btn-ghost btn-sm" href="/login">Log in</a></div>`;
      return;
    }
    container.innerHTML = `<div class="glass panel muted">Loading cases…</div>`;
    try {
      const status = $("#status-filter").value;
      const res = await PRVApi.listCases({ status: status || undefined, limit: 100 });
      state.cases = res.cases || [];
      if (!state.cases.length) {
        container.innerHTML = `<div class="glass panel muted">No cases ${status ? `with status ${esc(status)}` : "saved"} yet.
          Analyze something in <a href="/verify">VERIFY ANYTHING</a>, then save it as a case from the report page.</div>`;
        return;
      }
      container.innerHTML = state.cases.map(caseCard).join("");
      container.querySelectorAll("[data-case]").forEach((card) => {
        card.addEventListener("click", () => openCase(card.dataset.case));
      });
    } catch (err) {
      if (err.status === 401) {
        localStorage.removeItem("prv_token");
        state.authenticated = false;
        loadCases();
        return;
      }
      container.innerHTML = `<div class="glass panel error-panel">Failed to load cases: ${esc(err.message)}</div>`;
    }
  }

  function caseCard(c) {
    const sev = (c.severity_label || "LOW").toLowerCase();
    return `
      <article class="case-card glass" data-case="${c.case_id}" role="button" tabindex="0"
               aria-label="Open case ${esc(c.title)}">
        <header class="case-card-head">
          <span class="badge badge-${sev}">${esc(c.severity_label || "N/A")}</span>
          <span class="badge badge-status badge-status-${(c.status || "open").toLowerCase()}">${esc(c.status)}</span>
        </header>
        <h3>${esc(c.title)}</h3>
        <p class="muted">${esc(c.summary || "No summary yet.")}</p>
        <footer class="case-card-foot">
          <span title="Intervention priority at save time">Priority ${c.priority_snapshot != null ? Number(c.priority_snapshot).toFixed(0) : "—"}/100</span>
          <span>Job #${c.verification_job_id}</span>
          <span>${c.note_count ?? 0} note${(c.note_count ?? 0) === 1 ? "" : "s"}</span>
          <span class="muted">${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</span>
        </footer>
      </article>`;
  }

  // ------------------------------------------------------------------ detail
  async function openCase(caseId) {
    try {
      const detail = await PRVApi.caseDetail(caseId);
      let report = null;
      try { report = await PRVApi.verifyReport(detail.verification_job_id); } catch { }
      state.selected = detail;
      renderDetail(detail, report);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function renderDetail(c, report) {
    const box = $("#case-detail");
    box.classList.remove("hidden");
    const overall = (report && report.overall) || {};
    const notes = (c.notes || []).map((n) => `
      <li class="note">
        <header><strong>${esc(n.author)}</strong><span class="muted">${esc((n.created_at || "").slice(0, 16).replace("T", " "))}</span></header>
        <p>${esc(n.body)}</p>
      </li>`).join("");
    box.innerHTML = `
      <div class="glass panel">
        <header class="case-detail-head">
          <div>
            <h2>${esc(c.title)}</h2>
            <p class="muted">Job #${c.verification_job_id} · saved ${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</p>
          </div>
          <div class="case-detail-actions">
            <select id="case-status" class="select-input" aria-label="Case status">
              ${["OPEN", "MONITORING", "ESCALATED", "CLOSED"].map((s) =>
                `<option value="${s}" ${s === c.status ? "selected" : ""}>${s}</option>`).join("")}
            </select>
            <a class="btn btn-ghost btn-sm" href="/report/${c.verification_job_id}">🔎 OPEN REPORT</a>
            <button class="btn btn-danger btn-sm" id="case-delete" type="button">DELETE</button>
          </div>
        </header>
        ${c.summary ? `<p>${esc(c.summary)}</p>` : ""}
        <div class="case-stats">
          <div class="stat"><span class="label">PRIORITY</span><span class="value">${c.priority_snapshot != null ? Number(c.priority_snapshot).toFixed(0) : "—"}/100</span></div>
          <div class="stat"><span class="label">SEVERITY</span><span class="value">${esc(c.severity_label || "—")}</span></div>
          <div class="stat"><span class="label">VERDICT</span><span class="value">${esc(c.verdict_snapshot || overall.verdict || "—")}</span></div>
        </div>
        <h3>Investigator notes</h3>
        <ul class="notes-list">${notes || '<li class="muted">No notes yet.</li>'}</ul>
        <form id="note-form" class="note-form" aria-label="Add a note">
          <textarea id="note-body" class="text-input" rows="2" maxlength="8000"
                    placeholder="Add an investigator note…" required></textarea>
          <button class="btn btn-primary btn-sm" type="submit">ADD NOTE</button>
        </form>
      </div>`;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });

    $("#case-status").addEventListener("change", async (e) => {
      try {
        await PRVApi.updateCase(c.case_id, { status: e.target.value });
        toast(`Case → ${e.target.value}`, "success");
        loadCases();
      } catch (err) { toast(err.message, "error"); }
    });
    $("#case-delete").addEventListener("click", async () => {
      if (!window.confirm("Delete this case? Notes are removed with it.")) return;
      try {
        await PRVApi.deleteCase(c.case_id);
        toast("Case deleted", "success");
        box.classList.add("hidden");
        loadCases();
      } catch (err) { toast(err.message, "error"); }
    });
    $("#note-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = $("#note-body").value.trim();
      if (!body) return;
      try {
        await PRVApi.addCaseNote(c.case_id, body);
        $("#note-body").value = "";
        toast("Note added", "success");
        openCase(c.case_id);
      } catch (err) { toast(err.message, "error"); }
    });
  }

  // ------------------------------------------------------------------ boot
  document.addEventListener("DOMContentLoaded", async () => {
    $("#status-filter").addEventListener("change", loadCases);
    $("#reload-btn").addEventListener("click", loadCases);
    await loadAuth();
    await loadCases();
  });
})();

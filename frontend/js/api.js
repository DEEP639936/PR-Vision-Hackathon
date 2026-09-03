/* PR•VISION — REST API client (all dashboard data comes through here) */
"use strict";

const PRVApi = (() => {
  const BASE = "/api";

  function token() {
    try { return localStorage.getItem("prv_token") || ""; } catch { return ""; }
  }

  function authHeaders() {
    const t = token();
    return t ? { "Authorization": `Bearer ${t}` } : {};
  }

  async function request(path, options = {}) {
    const response = await fetch(`${BASE}${path}`, {
      headers: { "Accept": "application/json", ...authHeaders(),
                 ...(options.body ? { "Content-Type": "application/json" } : {}) },
      ...options,
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
      } catch { /* non-JSON error */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  /** Download an export as a file (auth goes in the header, not the URL). */
  async function downloadExport(jobId, format) {
    const response = await fetch(`${BASE}/verify/${jobId}/export.${format}`, {
      headers: { ...authHeaders() },
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { const p = await response.json(); if (p.detail) detail = p.detail; } catch { }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `prvision-report-${jobId}.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  return {
    downloadExport,
    health: () => request("/health"),
    platforms: () => request("/platforms"),
    dashboardSummary: () => request("/dashboard/summary"),
    highPriority: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.label) qs.set("label", params.label);
      if (params.platform) qs.set("platform", params.platform);
      if (params.limit) qs.set("limit", Math.min(params.limit, 100));
      return request(`/dashboard/high-priority?${qs}`);
    },
    trending: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.platform) qs.set("platform", params.platform);
      if (params.limit) qs.set("limit", Math.min(params.limit, 50));
      return request(`/dashboard/trending?${qs}`);
    },
    posts: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.platform) qs.set("platform", params.platform);
      if (params.search) qs.set("search", params.search);
      if (params.limit) qs.set("limit", params.limit);
      return request(`/posts?${qs}`);
    },
    post: (id) => request(`/posts/${id}`),
    postMetrics: (id, { windowMinutes, limit = 500 } = {}) => {
      const qs = new URLSearchParams();
      if (windowMinutes) qs.set("window_minutes", windowMinutes);
      qs.set("limit", limit);
      return request(`/posts/${id}/metrics?${qs}`);
    },
    postFeatures: (id, limit = 100) => request(`/posts/${id}/features?limit=${limit}`),
    postPrediction: (id, refresh = false) => request(`/posts/${id}/prediction?refresh=${refresh}`),
    postIntervention: (id) => request(`/posts/${id}/intervention-score`),
    postPropagation: (id, limit = 300) => request(`/posts/${id}/propagation?limit=${limit}`),
    generateDemo: (body = {}) => request("/demo/generate", { method: "POST", body: JSON.stringify(body) }),
    demoArchetypes: () => request("/demo/archetypes"),
    startIngestion: (body = {}) => request("/ingestion/start", { method: "POST", body: JSON.stringify(body) }),
    stopIngestion: () => request("/ingestion/stop", { method: "POST" }),
    ingestionStatus: () => request("/ingestion/status"),
    mlStatus: () => request("/ml/status"),
    mlTrain: () => request("/ml/train", { method: "POST" }),

    // ---- verification (multimodal) ----
    submitVerify: (formData) => {
      // multipart — overrides the JSON content-type
      return fetch(`${BASE}/verify`, { method: "POST", body: formData })
        .then(async (r) => {
          if (!r.ok) {
            let detail = `${r.status} ${r.statusText}`;
            try { const p = await r.json(); if (p.detail) detail = typeof p.detail === "string" ? p.detail : JSON.stringify(p.detail); } catch { }
            throw new Error(detail);
          }
          return r.json();
        });
    },
    verifyJobs: (limit = 20) => request(`/verify/jobs?limit=${limit}`),
    verifyJob: (id) => request(`/verify/${id}`),
    verifyReport: (id) => request(`/verify/${id}/report`),
    cancelVerify: (id) => request(`/verify/${id}`, { method: "DELETE" }),
    providerHealth: () => request("/evidence/providers"),
    providerKeys: () => request("/settings/provider-keys"),
    saveProviderKey: (provider, key) => request("/settings/provider-keys", { method: "POST", body: JSON.stringify({ provider, key }) }),
    clearProviderKey: (provider) => request(`/settings/provider-keys/${encodeURIComponent(provider)}`, { method: "DELETE" }),
    factcheckSearch: (claim) => request(`/factcheck/search?claim=${encodeURIComponent(claim)}`),
    sourceProfiles: () => request("/sources/profiles"),

    // ---- auth (spec #3) ----
    login: (email, password) => request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
    register: (email, displayName, password) => request("/auth/register", { method: "POST", body: JSON.stringify({ email, display_name: displayName, password }) }),
    logout: () => request("/auth/logout", { method: "POST" }),
    me: () => request("/auth/me"),

    // ---- investigation cases (spec #14) ----
    createCase: (jobId, title, summary, status = "OPEN") => request("/cases", { method: "POST", body: JSON.stringify({ verification_job_id: jobId, title, summary, status }) }),
    listCases: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.status) qs.set("status", params.status);
      if (params.limit) qs.set("limit", params.limit);
      return request(`/cases?${qs}`);
    },
    caseDetail: (id) => request(`/cases/${id}`),
    updateCase: (id, patch) => request(`/cases/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
    deleteCase: (id) => request(`/cases/${id}`, { method: "DELETE" }),
    addCaseNote: (id, body) => request(`/cases/${id}/notes`, { method: "POST", body: JSON.stringify({ body }) }),
    caseForJob: async (jobId) => {
      // find an existing case for a verification job
      const res = await request(`/cases?limit=200`);
      return (res.cases || []).find((c) => c.verification_job_id === Number(jobId)) || null;
    },

    // ---- alerts (spec #13) ----
    alerts: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.severity) qs.set("severity", params.severity);
      if (params.acknowledged !== undefined && params.acknowledged !== null) qs.set("acknowledged", params.acknowledged);
      if (params.limit) qs.set("limit", params.limit);
      return request(`/alerts?${qs}`);
    },
    alertSummary: () => request("/alerts/summary"),
    acknowledgeAlert: (id, note) => request(`/alerts/${id}/ack`, { method: "POST", body: JSON.stringify({ note: note || null }) }),
  };
})();

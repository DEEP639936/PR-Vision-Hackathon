/* PR•VISION — investigation report renderer (evidence-first, fully cited) */
"use strict";

(() => {
  const jobId = Number(window.location.pathname.split("/").pop());
  const $ = (id) => document.getElementById(id);
  const root = $("report-root");

  const VERDICT_CLASS = {
    "SUPPORTED": "v-supported", "LIKELY SUPPORTED": "v-likely-supported", "LIKELY_SUPPORTED": "v-likely-supported",
    "MIXED EVIDENCE": "v-mixed", "MIXED_EVIDENCE": "v-mixed",
    "MISLEADING": "v-misleading", "LIKELY MISLEADING": "v-misleading", "LIKELY_MISLEADING": "v-misleading",
    "CONTRADICTED": "v-contradicted", "SATIRE/PARODY": "v-satire", "OUTDATED": "v-outdated",
    "UNVERIFIED": "v-unverified", "INSUFFICIENT EVIDENCE": "v-insufficient",
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const vChip = (v) => v ? `<span class="verdict-chip ${VERDICT_CLASS[v] || "v-unverified"}">${esc(String(v).replace(/_/g, " "))}</span>` : "";
  const badge = (cls, label) => `<span class="badge badge-${cls}">${label}</span>`;
  const classBadge = (sc) => badge(
    { LIVE: "live", EXTERNAL_EVIDENCE: "external", DERIVED: "derived", MODEL_PREDICTION: "model", SIMULATED: "simulated" }[sc] || "derived",
    esc((sc || "DERIVED").replace("_", " ")));

  /* -------------------------------------------------------- polling */
  async function load() {
    try {
      const job = await PRVApi.verifyJob(jobId);
      if (job.status === "queued" || job.status === "running") {
        root.innerHTML = progressView(job);
        setTimeout(load, 2500);
        return;
      }
      if (job.status === "failed") {
        root.innerHTML = `<div class="jobs-panel glass" style="padding:30px;">
          <h2 style="margin-bottom:10px;">Verification failed</h2>
          <p style="color:var(--text-2)">${esc(job.error || "unknown error")}</p>
          <p style="margin-top:16px"><a href="/verify" style="color:var(--accent)">← Back to Verify Anything</a></p></div>`;
        return;
      }
      const report = await PRVApi.verifyReport(jobId);
      render(report);
    } catch (err) {
      $("loading-msg").textContent = `Failed to load: ${err.message}`;
    }
  }

  function progressView(job) {
    return `<div class="jobs-panel glass" style="padding:34px; text-align:center;">
      <div style="font-size:34px; margin-bottom:12px;">🔬</div>
      <h2 style="letter-spacing:0.06em;">VERIFYING…</h2>
      <p style="color:var(--text-2); margin:10px 0 4px;">Job #${job.job_id} — ${esc(job.stage || "working")}</p>
      <div class="progress-track" style="max-width:420px;margin:16px auto;">
        <div class="bar" style="width:${job.progress || 4}%"></div>
      </div>
      <p class="verify-note">Evidence retrieval, claim extraction and media forensics are running.
        This view refreshes automatically.</p>
    </div>`;
  }

  /* -------------------------------------------------------- render */
  function render(r) {
    $("pill-job").querySelector(".value").textContent = `#${r.job_id} DONE`;
    $("pill-job").querySelector(".dot").style.background = "var(--low)";
    $("last-update").textContent = new Date().toLocaleTimeString();

    const sections = [];
    sections.push(verdictBanner(r));
    sections.push(provenance(r));
    if (r.media?.length) sections.push(mediaForensics(r.media));
    sections.push(claimsSection(r.claims || []));
    if (r.numerical_checks?.length) sections.push(numericalSection(r.numerical_checks));
    sections.push(graphSection(r.graph));
    sections.push(timelineSection(r.timeline || []));
    sections.push(providersSection(r.providers));
    sections.push(footer(r));
    sections.push(workspace(r));

    root.innerHTML = sections.join("");
    const gEl = document.getElementById("evidence-graph");
    if (gEl && r.graph) EvidenceGraph.render(gEl, r.graph);
    wireWorkspace(r);
  }

  /* ------------------------------------------- workspace: cases + export */
  function workspace(r) {
    return `
    <section class="section-title">Investigation Workspace</section>
    <div class="glass workspace-panel" id="workspace-panel" style="padding:20px;">
      <div class="workspace-grid">
        <div class="workspace-block">
          <div class="ws-head">Save as case</div>
          <div id="case-area" class="muted">Checking session…</div>
        </div>
        <div class="workspace-block">
          <div class="ws-head">⬇️ EXPORT REPORT</div>
          <p class="muted" style="font-size:12px;margin:6px 0 10px;">
            PDF / JSON / CSV include claims, evidence, verdicts, priority and the
            Limitations &amp; provenance section.</p>
          <div class="export-row">
            <button class="btn btn-ghost btn-sm" data-export="pdf" type="button">PDF</button>
            <button class="btn btn-ghost btn-sm" data-export="json" type="button">JSON</button>
            <button class="btn btn-ghost btn-sm" data-export="csv" type="button">CSV</button>
          </div>
        </div>
      </div>
    </div>`;
  }

  function wireWorkspace(r) {
    // exports — auth rides in the header via PRVApi.downloadExport
    document.querySelectorAll("[data-export]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const fmt = btn.dataset.export;
        btn.disabled = true;
        btn.textContent = "…";
        try {
          await PRVApi.downloadExport(r.job_id, fmt);
          toastMsg(`Report exported as ${fmt.toUpperCase()}`, "success");
        } catch (err) {
          toastMsg(err.status === 401
            ? "Export requires sign-in — log in, then retry."
            : `Export failed: ${err.message}`, "error");
        } finally {
          btn.textContent = fmt === "pdf" ? "PDF" : fmt === "json" ? "JSON" : "CSV";
          btn.disabled = false;
        }
      });
    });

    const caseArea = document.getElementById("case-area");
    if (!caseArea) return;
    (async () => {
      let token = null;
      try { token = localStorage.getItem("prv_token"); } catch { }
      if (!token) {
        caseArea.classList.remove("muted");
        caseArea.innerHTML = `🔒 Saving analyses as cases requires an operator account.
          <a class="btn btn-ghost btn-sm" href="/login">Log in</a>
          <a class="btn btn-ghost btn-sm" href="/register">GET STARTED</a>`;
        return;
      }
      let existing = null;
      try { existing = await PRVApi.caseForJob(r.job_id); } catch { }
      if (existing) { renderExistingCase(caseArea, existing); return; }

      caseArea.classList.remove("muted");
      caseArea.innerHTML = `
        <form id="case-form" class="case-form">
          <input id="case-title" class="text-input" maxlength="255" required
                 placeholder="Case title (e.g. “Viral screenshot — fabricated quote”)" />
          <textarea id="case-summary" class="text-input" rows="2" maxlength="4000"
                    placeholder="Optional investigator summary…"></textarea>
          <button class="btn btn-primary btn-sm" type="submit">CREATE CASE</button>
        </form>`;
      document.getElementById("case-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const title = document.getElementById("case-title").value.trim();
        if (title.length < 3) { toastMsg("Give the case a title (3+ characters).", "error"); return; }
        const summary = document.getElementById("case-summary").value.trim();
        try {
          const created = await PRVApi.createCase(r.job_id, title, summary);
          toastMsg(`Case #${created.case_id} created`, "success");
          renderExistingCase(caseArea, created);
        } catch (err) {
          toastMsg(err.status === 401 ? "Session expired — log in again." : `Case failed: ${err.message}`, "error");
        }
      });
    })();
  }

  function renderExistingCase(el, c) {
    el.innerHTML = `
      <div class="case-linked">
        <span class="badge badge-status badge-status-${(c.status || "open").toLowerCase()}">${esc(c.status || "OPEN")}</span>
        <span><strong>${esc(c.title)}</strong> — case #${c.case_id}</span>
        <a class="btn btn-ghost btn-sm" href="/cases">MANAGE IN CASES</a>
      </div>`;
  }

  function toastMsg(message, kind) {
    const stack = document.querySelector(".toast-stack") || document.body;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 400); }, 4200);
  }

  function verdictBanner(r) {
    const overall = r.overall || {};
    const risk = r.risk || {};
    const prio = r.priority || {};
    const riskColor = risk.risk_level === "CRITICAL" ? "var(--critical)"
      : risk.risk_level === "HIGH" ? "var(--high)"
      : risk.risk_level === "MEDIUM" ? "var(--medium)" : "var(--low)";
    const prioColor = prio.label === "CRITICAL" ? "var(--critical)"
      : prio.label === "HIGH" ? "var(--high)"
      : prio.label === "MEDIUM" ? "var(--medium)" : "var(--low)";
    return `
    <section class="section-title">Verdict</section>
    <div class="report-head">
      <article class="glass verdict-banner">
        <h2>Evidence-Based Assessment</h2>
        <div class="vb-top">
          ${vChip(overall.verdict)}
          ${r.content ? classBadge(r.content.source_classification) : ""}
          ${r.content?.fetch_status && r.content.fetch_status !== "ok" ? `<span class="tag">fetch: ${esc(r.content.fetch_status)}</span>` : ""}
        </div>
        <p class="vb-detail">${esc(overall.detail || "")}</p>
        ${overall.caveats?.length ? `<ul class="vb-caveats">${overall.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      </article>
      <div class="gauges" style="flex-direction:column;">
        <div class="gauge">
          <div class="g-value" style="color:${riskColor}">${risk.misinformation_risk ?? "—"}</div>
          <div class="g-label">Misinformation Risk · ${esc(risk.risk_level || "—")} · conf ${risk.confidence ?? "—"}%</div>
        </div>
        <div class="gauge">
          <div class="g-value" style="color:${prioColor}">${prio.intervention_priority ?? "—"}</div>
          <div class="g-label">Intervention Priority · ${esc(prio.label || "")} / 100</div>
        </div>
      </div>
    </div>
    ${risk.components?.length ? `
      <details class="glass" style="padding:14px 18px; margin-top:12px;">
        <summary style="cursor:pointer; font-size:12.5px; color:var(--text-2); letter-spacing:0.05em;">
          RISK FUSION BREAKDOWN — every signal stays visible (spec #34)</summary>
        <div style="margin-top:10px;">
          ${risk.components.filter((c) => (c.weight || 0) > 0).map((c) => `
            <div class="signal-row">
              <span style="color:var(--text-2)">${esc(c.detail)}</span>
              <span style="font-family:var(--mono); font-size:12px;">risk ${c.risk} · weight ${c.weight}</span>
            </div>`).join("")}
        </div>
      </details>` : ""}`;
  }

  function provenance(r) {
    const c = r.content || {};
    const og = c.og_metadata || {};
    const chain = c.redirect_chain || [];
    return `
    <section class="section-title">Source Provenance</section>
    <div class="glass" style="padding:18px;">
      <div class="prov-grid">
        ${c.title ? provItem("Title", esc(c.title)) : ""}
        ${c.publisher ? provItem("Publisher", esc(c.publisher)) : ""}
        ${c.author ? provItem("Author", esc(c.author)) : ""}
        ${c.published_at ? provItem("Published At", esc(c.published_at)) : ""}
        ${c.updated_at ? provItem("Updated At", esc(c.updated_at)) : ""}
        ${c.original_url ? provItem("Original URL", `<a href="${esc(c.original_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${esc(shorten(c.original_url, 58))}</a>`, true) : ""}
        ${c.canonical_url && c.canonical_url !== c.original_url ? provItem("Canonical URL", `<span class="mono">${esc(shorten(c.canonical_url, 58))}</span>`, true) : ""}
        ${chain.length > 1 ? provItem("Redirect Chain", chain.map((u) => `<span class="mono" style="display:block">${esc(shorten(u, 52))}</span>`).join(" → "), true) : ""}
        ${c.file_meta?.sha256 ? provItem("SHA-256", `<span class="mono">${esc(c.file_meta.sha256.slice(0, 24))}…</span>`, true) : ""}
        ${provItem("Source Classification", classBadge(c.source_classification), true)}
        ${c.text_stats?.words ? provItem("Text Volume", `${c.text_stats.words} words · ~${c.text_stats.reading_time_min} min read`) : ""}
        ${og["og:image"] ? provItem("Preview Image", `<span class="mono">${esc(shorten(og["og:image"], 48))}</span>`, true) : ""}
      </div>
    </div>`;
  }

  function provItem(k, v, raw = false) {
    return `<div class="prov-item"><div class="k">${k}</div><div class="v ${raw ? "mono" : ""}">${v}</div></div>`;
  }

  function shorten(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n) + "…" : s; }

  function mediaForensics(mediaList) {
    const cards = mediaList.map((m) => {
      const a = m.analysis || {};
      const signals = a.manipulation_signals || a.forensics || [];
      return `
      <article class="glass media-card">
        <h3>${esc((m.media_type || "media").toUpperCase())} · ${esc(m.filename || "")}</h3>
        <div class="signal-row"><span>Manipulation risk (heuristics)</span><b style="font-family:var(--mono)">${m.manipulation_risk ?? "—"}</b></div>
        ${m.ai_generation_signal != null ? `
        <div class="signal-row"><span>AI-generation signal</span><b style="font-family:var(--mono)">${m.ai_generation_signal} · conf ${esc(m.ai_signal_confidence || "LOW")}</b></div>` : ""}
        ${a.width ? `<div class="signal-row"><span>Dimensions</span><span class="mono">${a.width}×${a.height} · ${(m.size_bytes / 1024).toFixed(0)} KB</span></div>` : ""}
        ${a.pages ? `<div class="signal-row"><span>Pages / images / links</span><span class="mono">${a.pages} / ${a.images ?? "—"} / ${a.links_count ?? "—"}</span></div>` : ""}
        ${a.exif && Object.keys(a.exif).length ? `<div class="signal-row"><span>EXIF keys</span><span class="mono">${esc(Object.keys(a.exif).slice(0, 5).join(", "))}</span></div>` : ""}
        ${m.ocr_text ? `
          <p style="font-size:11px;letter-spacing:.08em;color:var(--text-3);margin:12px 0 6px;">OCR TEXT (SPEC #15)</p>
          <div class="ocr-box">${esc(m.ocr_text.slice(0, 2400))}</div>` : ""}
        ${signals.length ? `
          <p style="font-size:11px;letter-spacing:.08em;color:var(--text-3);margin:12px 0 6px;">FORENSIC SIGNALS</p>
          ${signals.map((s) => `<div class="signal-row"><span style="color:var(--text-2)">${esc(s.signal)} — ${esc(s.note || s.detail || "")}</span></div>`).join("")}` : ""}
        ${a.metadata_anomalies?.length ? `
          <p style="font-size:11px;letter-spacing:.08em;color:var(--text-3);margin:12px 0 6px;">METADATA</p>
          ${a.metadata_anomalies.map((x) => `<div class="signal-row"><span style="color:var(--text-2)">${esc(x)}</span></div>`).join("")}` : ""}
        ${a.detectors_run?.length ? `<div class="claim-tags" style="margin-top:10px;">${a.detectors_run.map((d) => `<span class="tag">${esc(d)}</span>`).join("")}</div>` : ""}
        ${m.authenticity_note ? `<div class="authenticity">${esc(m.authenticity_note)}</div>` : ""}
      </article>`;
    }).join("");
    return `<section class="section-title">Media Forensics</section><div class="media-grid">${cards}</div>`;
  }

  function claimsSection(claims) {
    if (!claims.length) return `<section class="section-title">Claim Verification</section>
      <div class="glass" style="padding:20px;color:var(--text-2)">No claims extracted from this content.</div>`;
    const cards = claims.map((c) => {
      const v = c.verdict || {};
      const ev = c.evidence || [];
      const fcs = c.fact_checks || [];
      const sup = ev.filter((e) => e.stance === "supports");
      const con = ev.filter((e) => e.stance === "contradicts");
      const conf = Math.round((v.confidence || 0) * 100);
      return `
      <article class="glass claim-card">
        <div class="claim-head">
          <div>
            <div class="claim-num">CLAIM ${String(c.ordinal).padStart(2, "0")} · ${esc(c.claim_type)} ${c.checkable ? "" : "· NOT CHECKABLE"}</div>
            <div class="claim-text">${esc(c.text)}</div>
            <div class="claim-tags">
              ${c.time_context ? `<span class="tag">🕒 ${esc(c.time_context)}</span>` : ""}
              ${(c.entities || []).slice(0, 5).map((e) => `<span class="tag">${esc(e.name)}</span>`).join("")}
              ${v.temporal_flag ? `<span class="tag">temporal: ${esc(v.temporal_flag)}</span>` : ""}
              ${v.primary_source_available ? `<span class="tag">primary source available</span>` : ""}
              <span class="tag">extraction: ${esc(c.extraction_method || "heuristic")}</span>
            </div>
          </div>
          <div style="text-align:right;">
            ${vChip(v.verdict)}
            <div style="font-family:var(--mono); font-size:12px; color:var(--text-2); margin-top:6px;">confidence ${conf}%</div>
          </div>
        </div>
        ${v.verdict ? `<div class="confidence-bar"><div class="fill" style="width:${conf}%"></div></div>` : ""}
        ${v.explanation ? `<p class="claim-explain">${esc(v.explanation)}</p>` : ""}
        ${v.confidence_rationale ? `<p style="font-size:11.5px;color:var(--text-3);margin-top:6px;">Why this confidence: ${esc(v.confidence_rationale)}</p>` : ""}

        ${fcs.length ? `
          <div class="claim-evidence">
            ${fcs.map((f) => `
              <div class="ev-item">
                <span class="ev-stance stance-supports" style="background:rgba(251,191,36,.13);color:var(--medium)">FACT-CHECK</span>
                <div>
                  <div class="ev-title">${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.publisher || "Fact-check")}: ${esc(f.textual_rating || "review")}</a>` : `${esc(f.publisher || "")}: ${esc(f.textual_rating || "")}`}</div>
                  ${f.review_snippet ? `<div class="ev-snippet">${esc(f.review_snippet.slice(0, 220))}</div>` : ""}
                  <div class="ev-meta"><span>${esc(f.published_at || "date unavailable")}</span><span>${esc(f.provider)}</span></div>
                </div>
                ${classBadge("EXTERNAL_EVIDENCE")}
              </div>`).join("")}
          </div>` : ""}
        ${c.checkable && !fcs.length && ev.length >= 0 ? `
          <p style="font-size:11.5px; color:var(--text-3); margin-top:10px;">No matching external fact-check found — this does NOT mean the claim is true.</p>` : ""}

        ${ev.length ? `
          <div class="claim-evidence">
            ${[...con, ...sup].length ? evidenceGroup("CONTRADICTING EVIDENCE", con) : ""}
            ${evidenceGroup("SUPPORTING EVIDENCE", sup)}
            ${evidenceGroup("CONTEXT (NEUTRAL)", ev.filter((e) => e.stance === "neutral").slice(0, 3))}
          </div>` : ""}
        ${(c.numbers || []).length ? `
          <p style="font-size:11.5px;color:var(--text-3);margin-top:8px;">Numeric facts in claim: ${(c.numbers || []).map((n) => esc(n.raw)).join(" · ")}</p>` : ""}
      </article>`;
    }).join("");
    return `<section class="section-title">Claim-by-Claim Verification</section>${cards}`;
  }

  function evidenceGroup(title, items) {
    if (!items?.length) return "";
    return `
      <div>
        <p style="font-size:11px;letter-spacing:.08em;color:var(--text-3);margin:8px 0 6px;">${title} (${items.length})</p>
        ${items.slice(0, 5).map((e) => `
          <div class="ev-item">
            <span class="ev-stance stance-${esc(e.stance)}">${esc(e.stance)}</span>
            <div>
              <div class="ev-title">${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.title || e.url)}</a>` : esc(e.title || "evidence")}</div>
              ${e.snippet ? `<div class="ev-snippet">${esc(e.snippet.slice(0, 240))}</div>` : ""}
              <div class="ev-meta">
                <span>${esc(e.publisher || "unknown publisher")}</span>
                ${e.published_at ? `<span>${esc(e.published_at)}</span>` : ""}
                <span>via ${esc(e.provider)}</span>
              </div>
            </div>
            <div class="ev-quality">quality<br>${e.quality != null ? Number(e.quality).toFixed(2) : "—"}</div>
          </div>`).join("")}
      </div>`;
  }

  function numericalSection(checks) {
    const cards = checks.map((n) => `
      <div class="num-check">
        <span class="nc-status nc-${esc(n.status)}">${esc(n.status).toUpperCase()}</span>
        <div>
          <div class="nc-type">${esc(n.check_type.replace(/_/g, " "))} ${classBadge(n.source_classification || "DERIVED")}</div>
          <div class="nc-detail">${esc(n.detail || "")}</div>
          ${n.expected ? `<div style="font-size:11.5px;color:var(--text-3);margin-top:3px;" class="mono">expected ${esc(n.expected)} · observed ${esc(n.observed || "")}</div>` : ""}
        </div>
      </div>`).join("");
    return `<section class="section-title">Numerical Fact Checking — deterministic, no LLM arithmetic</section>${cards}`;
  }

  function graphSection(graph) {
    return `
    <section class="section-title">Evidence Graph</section>
    <div class="glass graph-wrap" id="evidence-graph"></div>`;
  }

  function timelineSection(events) {
    if (!events.length) return "";
    const rows = events.map((e) => {
      const t = e.occurred_at ? new Date(e.occurred_at).toLocaleString()
        : e.occurred_at_raw ? esc(e.occurred_at_raw) : "—";
      return `<div class="tl-event">
        <div class="tl-time">${t}</div>
        <div class="tl-label">${esc(e.label)}</div>
        ${e.detail ? `<div class="tl-detail">${esc(e.detail)}</div>` : ""}
        ${e.url ? `<div class="tl-detail"><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${esc(shorten(e.url, 70))}</a></div>` : ""}
      </div>`;
    }).join("");
    return `<section class="section-title">Evidence Timeline</section><div class="glass" style="padding:20px;"><div class="timeline">${rows}</div></div>`;
  }

  function providersSection(providers) {
    if (!providers) return "";
    return `
    <section class="section-title">Provider States — honest, never simulated</section>
    <div class="providers-strip">
      ${Object.entries(providers).filter(([k]) => k !== "note").map(([k, v]) => `
        <div class="provider-chip">
          <span class="dot" style="background:${v === "CONNECTED" ? "var(--low)" : v === "DISABLED" ? "var(--text-3)" : "var(--critical)"}"></span>
          <span>${esc(k)}</span><span class="pstate" style="color:var(--text-2)">${esc(v)}</span>
        </div>`).join("")}
      ${providers.note ? `<span style="font-size:11.5px;color:var(--text-3);align-self:center;">${esc(providers.note)}</span>` : ""}
    </div>`;
  }

  function footer(r) {
    const prio = r.priority || {};
    return `
    <div class="jobs-panel" style="margin-top:28px; padding-bottom:34px;">
      ${prio.factors?.length ? `
        <div class="glass" style="padding:14px 18px; margin-bottom:12px;">
          <div style="font-size:11px;letter-spacing:.08em;color:var(--text-3);margin-bottom:6px;">INTERVENTION PRIORITY FACTORS</div>
          ${prio.factors.map((f) => `<div class="signal-row"><span style="color:var(--text-2)">${esc(f.factor)}</span><b class="mono">+${f.contribution}</b></div>`).join("")}
        </div>` : ""}
      <p class="verify-note">PR•VISION is decision-support for human investigators. Every conclusion above is traceable to
        retrieved evidence; heuristics are labelled as such; the absence of evidence is never presented as proof.</p>
    </div>`;
  }

  load();
})();

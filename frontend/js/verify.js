/* PR•VISION — VERIFY ANYTHING page logic */
"use strict";

(() => {
  /* inline stroke SVG icons — replaced per input mode in the dropzone */
  const svg = (body) =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" width="28" height="28">${body}</svg>`;
  const ICONS = {
    url: svg('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
    text: svg('<line x1="17" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="17" y1="18" x2="3" y2="18"/>'),
    image: svg('<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>'),
    screenshot: svg('<rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/>'),
    pdf: svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),
    docx: svg('<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>'),
    csv: svg('<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/>'),
    html: svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'),
    json: svg('<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'),
  };

  const MODES = {
    url: { title: "Drop a link — or paste a URL", file: false,
           sub: "News articles, blog posts, public social posts. PR•VISION fetches only publicly accessible content — never bypassing auth, paywalls or robots." },
    text: { title: "Paste the text to investigate", file: false,
            sub: "A claim, a quote, a message forward, or an entire article body." },
    image: { title: "Drop an image to analyse", file: true,
             sub: "OCR + metadata + manipulation heuristics + optional vision-model signal. Extracted text is verified claim-by-claim." },
    screenshot: { title: "Drop a screenshot to analyse", file: true,
                  sub: "Treated as an evidence object: OCR, layout and UI cues, metadata — never assumed genuine." },
    pdf: { title: "Drop a PDF to analyse", file: true,
           sub: "Text, metadata, structure, links and forensic signals (date contradictions, odd producers, repeated sections)." },
    docx: { title: "Drop a DOC/DOCX to analyse", file: true,
            sub: "Text, structure and tables — claims verified like any article." },
    csv: { title: "Drop a CSV to analyse", file: true,
           sub: "Deterministic numeric verification: totals, growth rates, percentage bounds." },
  };

  let mode = "url";
  let chosenFile = null;

  const $ = (id) => document.getElementById(id);
  const dropzone = $("dropzone");

  /* ------------------------------------------------------------ tabs */
  document.querySelectorAll(".mode-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".mode-tabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      mode = btn.dataset.mode;
      const cfg = MODES[mode];
      $("dz-icon").innerHTML = ICONS[mode] || ICONS.url;
      $("dz-title").textContent = cfg.title;
      $("dz-sub").textContent = cfg.sub;
      $("row-url").classList.toggle("visible", mode === "url");
      $("row-text").classList.toggle("visible", mode === "text");
      if (cfg.file) dropzone.setAttribute("data-file", "1");
      else dropzone.removeAttribute("data-file");
    });
  });

  /* -------------------------------------------------------- dropzone */
  dropzone.addEventListener("click", () => {
    if (MODES[mode].file) $("file-input").click();
  });
  dropzone.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && MODES[mode].file) { e.preventDefault(); $("file-input").click(); }
  });
  $("file-input").addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (f) setFile(f);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f && MODES[mode].file) setFile(f);
    else if (f) setFile(f); // dropped a file in any mode — accept it
  });
  // paste URLs / text anywhere on the page
  document.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData).getData("text") || "";
    if (!text || document.activeElement === $("input-text") || document.activeElement === $("input-url")) return;
    if (/^https?:\/\//i.test(text.trim()) && !text.includes("\n")) {
      document.querySelector('[data-mode="url"]').click();
      $("input-url").value = text.trim();
    } else if (text.trim().length > 20) {
      document.querySelector('[data-mode="text"]').click();
      $("input-text").value = text.trim();
    }
  });

  function setFile(f) {
    chosenFile = f;
    $("dz-title").textContent = `Ready: ${f.name}`;
    $("dz-sub").textContent = `${(f.size / 1024).toFixed(1)} KB — press Start Analysis`;
  }

  /* ---------------------------------------------------------- submit */
  $("btn-verify").addEventListener("click", async () => {
    const btn = $("btn-verify");
    const note = $("verify-note");
    try {
      btn.disabled = true;
      const fd = new FormData();
      if (chosenFile) {
        fd.append("file", chosenFile);
      } else if (mode === "url") {
        const u = $("input-url").value.trim();
        if (!u) { note.textContent = "Paste a URL first."; return; }
        fd.append("url", u);
      } else {
        const t = $("input-text").value.trim();
        if (!t) { note.textContent = "Paste some text first."; return; }
        fd.append("text", t);
      }
      note.textContent = "Submitting…";
      const res = await PRVApi.submitVerify(fd);
      note.textContent = `Job #${res.job_id} queued — opening the live report…`;
      window.location.href = `/report/${res.job_id}`;
    } catch (err) {
      note.textContent = `${err.message}`;
    } finally {
      btn.disabled = false;
      chosenFile = null;
      $("file-input").value = "";
    }
  });

  /* -------------------------------------------------------- helpers */
  const VERDICT_CLASS = {
    "SUPPORTED": "v-supported", "LIKELY SUPPORTED": "v-likely-supported", "LIKELY_SUPPORTED": "v-likely-supported",
    "MIXED EVIDENCE": "v-mixed", "MIXED_EVIDENCE": "v-mixed",
    "MISLEADING": "v-misleading", "LIKELY MISLEADING": "v-misleading", "LIKELY_MISLEADING": "v-misleading",
    "CONTRADICTED": "v-contradicted", "SATIRE/PARODY": "v-satire", "OUTDATED": "v-outdated",
    "UNVERIFIED": "v-unverified", "INSUFFICIENT EVIDENCE": "v-insufficient",
  };
  const KIND_ICON = ICONS;

  function verdictChip(verdict) {
    if (!verdict) return "";
    const cls = VERDICT_CLASS[verdict] || "v-unverified";
    return `<span class="verdict-chip ${cls}">${escapeHtml(verdict)}</span>`;
  }

  /* ----------------------------------------------------------- jobs */
  async function refreshJobs() {
    try {
      const data = await PRVApi.verifyJobs(12);
      const list = $("job-list");
      if (!data.jobs.length) {
        list.innerHTML = `<p style="color:var(--text-3);font-size:13px;padding:8px 2px;">No verifications yet — submit something above.</p>`;
        return;
      }
      list.innerHTML = data.jobs.map((j) => {
        const v = j.result_summary?.verdict;
        const pct = j.status === "completed" ? 100 : (j.progress || 0);
        const sub = [j.input_kind, j.result_summary?.evidence_count != null ? `${j.result_summary.evidence_count} evidence items` : null,
                     j.result_summary?.claims_total != null ? `${j.result_summary.claims_total} claims` : null]
          .filter(Boolean).join(" · ");
        return `
        <a class="job-row" href="/report/${j.job_id}">
          <div class="job-icon">${KIND_ICON[j.input_kind] || ICONS.url}</div>
          <div>
            <div class="job-label">#${j.job_id} — ${escapeHtml(j.input_label || "input")}</div>
            <div class="job-meta">${sub || j.stage || j.status}</div>
          </div>
          ${j.status === "completed" ? verdictChip(v || "UNVERIFIED")
            : j.status === "failed" ? `<span class="verdict-chip v-contradicted">FAILED</span>`
            : `<span class="verdict-chip v-unverified">${j.status.toUpperCase()}</span>`}
          <div class="job-pct">${pct}%</div>
          ${j.status === "running" || j.status === "queued"
            ? `<div class="progress-track"><div class="bar" style="width:${pct}%"></div></div>` : ""}
        </a>`;
      }).join("");
    } catch { /* transient */ }
  }

  /* ----------------------------------------------------- providers */
  const KEYED_PROVIDERS = {
    google_factcheck: {
      label: "GOOGLE FACT CHECK TOOLS API",
      how: "console.cloud.google.com → enable “Fact Check Tools API” → APIs & Services → Credentials → Create credentials → API key",
      link: "https://console.cloud.google.com/apis/library/factchecktools.googleapis.com",
      linkText: "Open Google Cloud Console",
    },
    newsapi: {
      label: "NEWSAPI",
      how: "newsapi.org → register a free account → copy the API key from your dashboard",
      link: "https://newsapi.org/register",
      linkText: "Open newsapi.org/register",
    },
  };

  async function refreshProviders() {
    try {
      const data = await PRVApi.providerHealth();
      $("providers-strip").innerHTML = data.providers.map((p) => {
        const color = p.state === "CONNECTED" ? "var(--low)"
          : p.state === "DEGRADED" ? "var(--medium)"
          : p.state === "DISABLED" ? "var(--text-3)" : "var(--critical)";
        const keyed = KEYED_PROVIDERS[p.name] ? " keyed" : "";
        const manage = KEYED_PROVIDERS[p.name]
          ? `<button class="pkey-btn" data-provider="${escapeHtml(p.name)}" type="button"
               title="Set your API key to enable this provider">⚙ KEY</button>` : "";
        return `<div class="provider-chip${keyed}" title="${escapeHtml(p.detail || p.state)}">
          <span class="dot" style="background:${color}"></span>
          <span>${escapeHtml(p.name)}</span>
          <span class="pstate" style="color:${color}">${escapeHtml(p.state)}</span>
          ${manage}
        </div>`;
      }).join("");
    } catch { /* transient */ }
  }

  /* -------------------------------------- provider key form (save & test) */
  let openKeyProvider = null;

  function closeKeyForm() {
    openKeyProvider = null;
    const el = $("provider-key-form");
    if (el) el.remove();
  }

  function renderKeyForm(provider, info) {
    closeKeyForm();
    openKeyProvider = provider;
    const strip = $("providers-strip");
    const form = document.createElement("div");
    form.className = "provider-key-form";
    form.id = "provider-key-form";
    form.innerHTML = `
      <div class="pkf-head">
        <span class="pkf-title">${escapeHtml(info.label)} — SET YOUR FREE API KEY</span>
        <button class="pkf-close" type="button" aria-label="Close">✕</button>
      </div>
      <p class="pkf-how">How to get it: ${escapeHtml(info.how)}.
        <a href="${info.link}" target="_blank" rel="noopener noreferrer">${escapeHtml(info.linkText)}</a></p>
      <div class="pkf-row">
        <input id="pkf-input" type="password" autocomplete="off" spellcheck="false"
               placeholder="Paste your API key here" />
        <button id="pkf-save" class="btn btn-primary btn-sm" type="button">SAVE &amp; TEST</button>
      </div>
      <div id="pkf-result" class="pkf-result" role="status"></div>
      <p class="pkf-note">The key is stored on this server in the project .env file and is
        never displayed again. The status shown after saving comes from a real test call —
        the provider only turns CONNECTED if the key works.</p>`;
    strip.parentElement.appendChild(form);

    form.querySelector(".pkf-close").addEventListener("click", closeKeyForm);
    form.querySelector("#pkf-save").addEventListener("click", async () => {
      const input = form.querySelector("#pkf-input");
      const result = form.querySelector("#pkf-result");
      const key = input.value.trim();
      if (!key) { result.textContent = "Paste a key first."; return; }
      result.textContent = "Testing key with a real call…";
      result.className = "pkf-result pending";
      try {
        const resp = await PRVApi.saveProviderKey(provider, key);
        if (resp.ok === false) {
          result.textContent = `Not saved: ${resp.error || "unknown error"}`;
          result.className = "pkf-result error";
          return;
        }
        result.textContent = `${resp.provider}: ${resp.state}` +
          (resp.detail ? ` — ${resp.detail}` : "");
        result.className = "pkf-result " +
          (resp.state === "CONNECTED" ? "ok" : "error");
        refreshProviders();
      } catch (err) {
        result.textContent = err.status === 401 || /401|Authentication/i.test(err.message)
          ? "Sign in first to manage provider keys."
          : `Failed: ${err.message}`;
        result.className = "pkf-result error";
      }
    });
    form.querySelector("#pkf-input").focus();
  }

  function wireProviderStrip() {
    const strip = $("providers-strip");
    strip.addEventListener("click", (e) => {
      const btn = e.target.closest(".pkey-btn");
      if (!btn) return;
      const provider = btn.dataset.provider;
      if (openKeyProvider === provider) { closeKeyForm(); return; }
      renderKeyForm(provider, KEYED_PROVIDERS[provider]);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && openKeyProvider) closeKeyForm();
    });
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  refreshJobs();
  refreshProviders();
  wireProviderStrip();
  setInterval(refreshJobs, 4000);
  setInterval(refreshProviders, 15000);
})();

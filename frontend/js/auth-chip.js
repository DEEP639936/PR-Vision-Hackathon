/* PR•VISION — shared auth chip for the top bar (all app pages) */
"use strict";

(() => {
  async function renderChip() {
    const area = document.getElementById("auth-area");
    if (!area) return;
    let token = null;
    try { token = localStorage.getItem("prv_token"); } catch { }
    if (!token) {
      area.dataset.auth = "signed-out";
      area.innerHTML = '<a class="btn btn-ghost btn-sm" href="/login">Log in</a>';
      return;
    }
    try {
      const res = await fetch("/api/auth/me", { headers: { "Authorization": `Bearer ${token}` } });
      if (!res.ok) throw new Error("stale");
      const { user } = await res.json();
      area.dataset.auth = "signed-in";
      area.innerHTML = `
        <span class="user-chip" title="${user.email}">
          <span class="dot"></span>${user.display_name || user.email} · ${user.role}
        </span>
        <button class="btn btn-ghost btn-sm" id="chip-logout" type="button">Log out</button>`;
      const btn = document.getElementById("chip-logout");
      btn.addEventListener("click", async () => {
        try {
          await fetch("/api/auth/logout", { method: "POST", headers: { "Authorization": `Bearer ${token}` } });
        } catch { /* already invalid */ }
        localStorage.removeItem("prv_token");
        localStorage.removeItem("prv_user");
        window.location.assign("/login");
      });
    } catch {
      localStorage.removeItem("prv_token");
      localStorage.removeItem("prv_user");
      area.dataset.auth = "signed-out";
      area.innerHTML = '<a class="btn btn-ghost btn-sm" href="/login">Log in</a>';
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderChip);
  } else {
    renderChip();
  }
})();

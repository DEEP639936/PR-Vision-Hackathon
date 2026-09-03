/* PR•VISION — auth flows (login + register).
   Self-contained: mirrors api.js conventions (const BASE = "/api", error
   detail extraction, .status on thrown errors) without importing it.
   Session storage: localStorage "prv_token" (bearer token) + "prv_user" (user JSON). */
"use strict";

const PRVAuth = (() => {
  const BASE = "/api";
  const TOKEN_KEY = "prv_token";
  const USER_KEY = "prv_user";
  const DASHBOARD = "/dashboard";

  /* ---------------------------------------------- tiny fetch wrapper ---- */
  async function request(path, { method = "GET", body, auth = false } = {}) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth) headers["Authorization"] = `Bearer ${localStorage.getItem(TOKEN_KEY) || ""}`;

    const response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) {
          detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
        }
      } catch { /* non-JSON error body */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  /* ------------------------------------------------------ session ops --- */
  function storeSession(data, fallbackEmail) {
    if (!data || !data.access_token) {
      throw new Error("Authentication response was missing an access token.");
    }
    localStorage.setItem(TOKEN_KEY, data.access_token);
    const user = data.user || (fallbackEmail ? { email: fallbackEmail } : {});
    localStorage.setItem(USER_KEY, JSON.stringify(user));
    window.location.assign(DASHBOARD);
  }

  function clearStaleSession() {
    try {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    } catch { /* storage unavailable — nothing to clear */ }
  }

  /* If a token already exists, validate it against /auth/me:
     valid → straight to the dashboard; 401/403 → clear stale keys silently. */
  async function bootstrap() {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) return;
    try {
      await request("/auth/me", { auth: true });
      window.location.assign(DASHBOARD);
    } catch (err) {
      if (err.status === 401 || err.status === 403) clearStaleSession();
      /* network / 5xx: stay on the page so the user can retry */
    }
  }

  /* --------------------------------------------------- DOM helpers ------ */
  const $ = (id) => document.getElementById(id);

  function setError(el, message) {
    if (!el) return;
    if (message) {
      el.textContent = message;
      el.hidden = false;
    } else {
      el.textContent = "";
      el.hidden = true;
    }
  }

  function setBusy(btn, busyText, busy) {
    if (!btn) return;
    const label = btn.querySelector(".btn-label");
    btn.disabled = busy;
    btn.classList.toggle("loading", busy);
    if (label) {
      if (busy) {
        btn.dataset.idleText = label.textContent;
        label.textContent = busyText;
      } else if (btn.dataset.idleText) {
        label.textContent = btn.dataset.idleText;
      }
    }
  }

  function markInvalid(input, invalid) {
    if (input) input.setAttribute("aria-invalid", invalid ? "true" : "false");
  }

  function wirePasswordToggle(toggleId, input) {
    const btn = $(toggleId);
    if (!btn || !input) return;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", String(show));
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      const eyeIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
      const eyeOffIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
      btn.innerHTML = show ? eyeOffIcon : eyeIcon;
      input.focus({ preventScroll: true });
    });
  }

  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  /* ------------------------------------------------------ login page ---- */
  function wireLoginForm() {
    const form = $("login-form");
    if (!form) return false;

    const email = $("login-email");
    const password = $("login-password");
    const emailErr = $("login-email-error");
    const pwErr = $("login-password-error");
    const formErr = $("form-error");
    const submit = $("login-submit");

    wirePasswordToggle("login-pw-toggle", password);

    const demoBtn = $("demo-fill");
    if (demoBtn) {
      demoBtn.addEventListener("click", () => {
        if (email) email.value = "demo@prvision.ai";
        if (password) password.value = "DemoVision!2026";
        setError(emailErr, null);
        setError(pwErr, null);
        setError(formErr, null);
        markInvalid(email, false);
        markInvalid(password, false);
        if (email) email.focus();
        demoBtn.textContent = "FILLED ✓";
        setTimeout(() => { demoBtn.textContent = "FILL"; }, 1600);
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setError(emailErr, null);
      setError(pwErr, null);
      setError(formErr, null);

      const emailVal = email ? email.value.trim() : "";
      const pwVal = password ? password.value : "";
      let valid = true;

      if (!emailVal) {
        setError(emailErr, "Enter your analyst email.");
        markInvalid(email, true);
        valid = false;
      } else if (!EMAIL_RE.test(emailVal)) {
        setError(emailErr, "Enter a valid email address.");
        markInvalid(email, true);
        valid = false;
      }
      if (!pwVal) {
        setError(pwErr, "Enter your password.");
        markInvalid(password, true);
        valid = false;
      }
      if (!valid) {
        (emailErr && !emailErr.hidden ? email : password)?.focus();
        return;
      }

      setBusy(submit, "AUTHENTICATING…", true);
      try {
        const data = await request("/auth/login", {
          method: "POST",
          body: { email: emailVal, password: pwVal },
        });
        storeSession(data, emailVal); // redirects to /dashboard on success
      } catch (err) {
        setBusy(submit, null, false);
        if (err.status === 400 || err.status === 401) {
          setError(formErr, "Invalid credentials. Check your email and password, then try again.");
        } else {
          setError(formErr, `Login failed — ${err.message}`);
        }
      }
    });
    return true;
  }

  /* --------------------------------------------------- register page ---- */
  function wireRegisterForm() {
    const form = $("register-form");
    if (!form) return false;

    const name = $("register-name");
    const email = $("register-email");
    const password = $("register-password");
    const nameErr = $("register-name-error");
    const emailErr = $("register-email-error");
    const pwErr = $("register-password-error");
    const formErr = $("form-error");
    const submit = $("register-submit");

    wirePasswordToggle("register-pw-toggle", password);

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setError(nameErr, null);
      setError(emailErr, null);
      setError(pwErr, null);
      setError(formErr, null);

      const nameVal = name ? name.value.trim() : "";
      const emailVal = email ? email.value.trim() : "";
      const pwVal = password ? password.value : "";
      let valid = true;

      if (nameVal.length < 2) {
        setError(nameErr, "Enter a display name (at least 2 characters).");
        markInvalid(name, true);
        valid = false;
      }
      if (!emailVal) {
        setError(emailErr, "Enter your email address.");
        markInvalid(email, true);
        valid = false;
      } else if (!EMAIL_RE.test(emailVal)) {
        setError(emailErr, "Enter a valid email address.");
        markInvalid(email, true);
        valid = false;
      }
      if (pwVal.length < 8) {
        setError(pwErr, "Password must be at least 8 characters.");
        markInvalid(password, true);
        valid = false;
      }
      if (!valid) {
        const firstInvalid = form.querySelector('[aria-invalid="true"]');
        if (firstInvalid) firstInvalid.focus();
        return;
      }

      setBusy(submit, "SUBMITTING REQUEST…", true);
      try {
        const data = await request("/auth/register", {
          method: "POST",
          body: { email: emailVal, display_name: nameVal, password: pwVal },
        });
        storeSession(data, emailVal); // redirects to /dashboard on success
      } catch (err) {
        setBusy(submit, null, false);
        if (err.status === 409) {
          setError(formErr, "Email already registered. Try logging in instead.");
        } else if (err.status === 422) {
          setError(formErr, `Registration rejected — ${err.message}`);
        } else {
          setError(formErr, `Registration failed — ${err.message}`);
        }
      }
    });
    return true;
  }

  /* --------------------------------------------------------------- init -- */
  function init() {
    const wired = wireLoginForm() | wireRegisterForm(); // bitwise-intentional: run both
    if (wired) bootstrap();
  }

  init();

  return { init, request, TOKEN_KEY, USER_KEY };
})();

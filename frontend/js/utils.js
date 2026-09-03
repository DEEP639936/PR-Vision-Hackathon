/* PR•VISION — shared utilities: formatting, DOM helpers, count-up animation */
"use strict";

const PRVUtils = (() => {
  /** Compact number formatting: 12,540 → "12.5K" (keeps <1000 plain). */
  function compact(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    if (Math.abs(n) < 1000) return String(Math.round(n * 10) / 10);
    if (Math.abs(n) < 1_000_000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
    return (n / 1_000_000).toFixed(2) + "M";
  }

  function withCommas(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return Math.round(n).toLocaleString("en-US");
  }

  function pct(n, digits = 0) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return (n * 100).toFixed(digits) + "%";
  }

  function timeAgo(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - then);
    const s = Math.floor(diff / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  function clockTime(dateLike) {
    const d = dateLike ? new Date(dateLike) : new Date();
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  /** Truncate text for table cells. */
  function truncate(text, max = 90) {
    if (!text) return "";
    return text.length > max ? text.slice(0, max).trimEnd() + "…" : text;
  }

  /** Animated counter (respects prefers-reduced-motion). */
  function countUp(el, target, { duration = 700, decimals = 0, suffix = "" } = {}) {
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const from = parseFloat(el.dataset.currentValue || "0") || 0;
    if (reduce || Math.abs(target - from) < 0.01) {
      el.textContent = formatTarget(target);
      el.dataset.currentValue = String(target);
      return;
    }
    const start = performance.now();
    function formatTarget(v) {
      if (decimals > 0) return v.toFixed(decimals) + suffix;
      return Math.round(v).toLocaleString("en-US") + suffix;
    }
    function frame(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const value = from + (target - from) * eased;
      el.textContent = formatTarget(value);
      if (t < 1) requestAnimationFrame(frame);
      else el.dataset.currentValue = String(target);
    }
    requestAnimationFrame(frame);
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const child of children) if (child) node.appendChild(child);
    return node;
  }

  function debounce(fn, ms = 350) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  const PLATFORM_LABELS = {
    x: "X",
    reddit: "Reddit",
    instagram: "Instagram",
    facebook: "Facebook",
    linkedin: "LinkedIn",
    mastodon: "Mastodon",
    hackernews: "Hacker News",
    demo: "demo",
  };

  function platformLabel(slug) {
    return PLATFORM_LABELS[slug] || slug;
  }

  return { compact, withCommas, pct, timeAgo, clockTime, truncate, countUp, el, debounce, platformLabel };
})();

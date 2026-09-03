/* PR•VISION — landing page: network visualization, reveal-on-scroll.
   Self-contained (no dependency on api.js/utils.js). Vanilla JS only. */
"use strict";

const PRVLanding = (() => {
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

  function debounce(fn, ms = 120) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  /* --------------------------------------------- reveal on scroll ------- */
  function initReveal() {
    const els = document.querySelectorAll(".reveal");
    if (!els.length) return;
    document.documentElement.classList.add("js"); // gate hidden states behind JS availability
    if (REDUCED.matches || !("IntersectionObserver" in window)) {
      els.forEach((n) => n.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      }
    }, { threshold: 0.1, rootMargin: "0px 0px -36px 0px" });
    els.forEach((n) => io.observe(n));
  }

  /* ------------------------------------- hero network visualization ----- */
  function initNetwork() {
    const canvas = document.getElementById("net-canvas");
    if (!canvas || !(canvas instanceof HTMLCanvasElement)) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const TONE_NODE = "74, 140, 255";   // cyan accent
    const TONE_EDGE = "148, 163, 184";  // slate dim
    const TONE_ALERT_A = "248, 113, 113"; // critical red
    const TONE_ALERT_B = "251, 191, 36";  // amber
    const LINK_DIST = 118;
    const ALERT_COUNT = 3;

    let width = 0;
    let height = 0;
    let nodes = [];
    let alerts = [];
    let pulses = [];
    let rings = [];
    let rafId = 0;
    let running = false;
    let inView = true;
    let lastT = 0;

    const rand = (a, b) => a + Math.random() * (b - a);

    function buildNodes() {
      const target = Math.max(28, Math.min(64, Math.round((width * height) / 15500)));
      nodes = [];
      for (let i = 0; i < target; i++) {
        nodes.push({
          x: rand(4, Math.max(5, width - 4)),
          y: rand(4, Math.max(5, height - 4)),
          vx: rand(-0.045, 0.045),
          vy: rand(-0.045, 0.045),
          r: rand(1.1, 2.3),
          phase: rand(0, Math.PI * 2),
          alert: false,
          tone: TONE_NODE,
          nextEmit: 0,
        });
      }
      // mark 2–3 well-spread "alert" nodes (red/amber)
      alerts = [];
      const want = Math.min(ALERT_COUNT, nodes.length);
      let guard = 0;
      while (alerts.length < want && guard < 400) {
        guard++;
        const n = nodes[Math.floor(Math.random() * nodes.length)];
        if (alerts.includes(n)) continue;
        const minSep = Math.min(width, height) * 0.3;
        const far = alerts.every((a) => Math.hypot(a.x - n.x, a.y - n.y) > minSep);
        if (far || guard > 300) {
          n.alert = true;
          n.tone = alerts.length % 2 === 0 ? TONE_ALERT_A : TONE_ALERT_B;
          n.nextEmit = rand(500, 2800);
          alerts.push(n);
        }
      }
    }

    function resize() {
      const host = canvas.parentElement || canvas;
      const rect = host.getBoundingClientRect();
      const w = Math.max(1, rect.width);
      const h = Math.max(1, rect.height);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const oldW = width;
      const oldH = height;
      width = w;
      height = h;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!nodes.length || !oldW || !oldH) {
        buildNodes();
      } else {
        const sx = w / oldW;
        const sy = h / oldH;
        for (const n of nodes) {
          n.x = Math.max(2, Math.min(w - 2, n.x * sx));
          n.y = Math.max(2, Math.min(h - 2, n.y * sy));
        }
      }
      if (REDUCED.matches) drawStatic();
    }

    function neighborOf(source) {
      const near = [];
      for (const n of nodes) {
        if (n === source) continue;
        if (Math.hypot(n.x - source.x, n.y - source.y) <= LINK_DIST) near.push(n);
      }
      const pool = near.length ? near : nodes;
      return pool.length ? pool[Math.floor(Math.random() * pool.length)] : null;
    }

    function draw(t) {
      ctx.clearRect(0, 0, width, height);

      // edges between nearby nodes
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK_DIST * LINK_DIST) continue;
          const d = Math.sqrt(d2);
          const hot = a.alert || b.alert;
          const tone = a.alert ? a.tone : b.alert ? b.tone : TONE_EDGE;
          const alpha = (1 - d / LINK_DIST) * (hot ? 0.3 : 0.15);
          ctx.strokeStyle = `rgba(${tone}, ${alpha.toFixed(3)})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // nodes
      for (const n of nodes) {
        if (n.alert) {
          const pulse = 0.5 + 0.5 * Math.sin(t / 620 + n.phase);
          const glowR = (n.r + 5 + pulse * 4) * 2.2;
          const g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, glowR);
          g.addColorStop(0, `rgba(${n.tone}, ${(0.3 - pulse * 0.12).toFixed(3)})`);
          g.addColorStop(1, `rgba(${n.tone}, 0)`);
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(n.x, n.y, glowR, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = `rgba(${n.tone}, 0.95)`;
        } else {
          ctx.fillStyle = `rgba(${TONE_NODE}, 0.55)`;
        }
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fill();
      }

      // pulses travelling along edges
      for (const p of pulses) {
        ctx.fillStyle = `rgba(${p.tone}, ${(0.9 * (1 - p.t)).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.9, 0, Math.PI * 2);
        ctx.fill();
      }

      // expanding rings
      for (const r of rings) {
        ctx.strokeStyle = `rgba(${r.tone}, ${((1 - r.t) * 0.5).toFixed(3)})`;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.arc(r.x, r.y, r.r, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    function step(now) {
      const dt = Math.min(50, lastT ? now - lastT : 16);
      lastT = now;

      for (const n of nodes) {
        n.x += n.vx * (dt / 16.7);
        n.y += n.vy * (dt / 16.7);
        if (n.x < 5 || n.x > width - 5) n.vx *= -1;
        if (n.y < 5 || n.y > height - 5) n.vy *= -1;
        n.x = Math.max(2, Math.min(width - 2, n.x));
        n.y = Math.max(2, Math.min(height - 2, n.y));
      }

      for (const a of alerts) {
        a.nextEmit -= dt;
        if (a.nextEmit <= 0 && nodes.length > 1) {
          a.nextEmit = rand(2000, 3800);
          const target = neighborOf(a);
          if (target) {
            pulses.push({ from: a, to: target, t: 0, speed: 1 / rand(900, 1400), tone: a.tone, x: a.x, y: a.y });
          }
        }
      }

      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        p.t += p.speed * dt;
        if (p.t >= 1) {
          rings.push({ x: p.to.x, y: p.to.y, r: 3, t: 0, tone: p.tone });
          pulses.splice(i, 1);
          continue;
        }
        p.x = p.from.x + (p.to.x - p.from.x) * p.t;
        p.y = p.from.y + (p.to.y - p.from.y) * p.t;
      }

      for (let i = rings.length - 1; i >= 0; i--) {
        const r = rings[i];
        r.t += dt / 1100;
        r.r += dt * 0.045;
        if (r.t >= 1) rings.splice(i, 1);
      }

      draw(now);
    }

    function loop(now) {
      if (!running) return;
      step(now);
      rafId = requestAnimationFrame(loop);
    }

    function start() {
      if (running || REDUCED.matches || document.hidden || !inView) return;
      running = true;
      lastT = 0;
      rafId = requestAnimationFrame(loop);
    }

    function stop() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
    }

    function drawStatic() {
      stop();
      pulses.length = 0;
      rings.length = 0;
      for (const a of alerts) rings.push({ x: a.x, y: a.y, r: 10, t: 0.35, tone: a.tone });
      draw(0);
      rings.length = 0;
    }

    resize();
    if (REDUCED.matches) {
      drawStatic();
    } else {
      start();
    }

    const onResize = debounce(resize, 120);
    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else start();
    });
    if ("IntersectionObserver" in window) {
      const vio = new IntersectionObserver((entries) => {
        inView = entries[0].isIntersecting;
        if (inView) start();
        else stop();
      }, { threshold: 0.02 });
      vio.observe(canvas);
    }
    const onMotionPrefChange = (e) => (e.matches ? drawStatic() : start());
    if (typeof REDUCED.addEventListener === "function") REDUCED.addEventListener("change", onMotionPrefChange);
    else if (typeof REDUCED.addListener === "function") REDUCED.addListener(onMotionPrefChange);
  }

  /* -------------------------------------------------------------- init -- */
  function safe(fn) {
    try {
      fn();
    } catch (err) {
      console.warn("[PR•VISION] landing init:", err && err.message ? err.message : err);
    }
  }

  safe(initReveal);
  safe(initNetwork);

  return { initReveal, initNetwork };
})();

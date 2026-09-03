/* PR•VISION — interactive evidence graph (spec #9)
 * Canvas force-directed graph: zoom, pan, node selection, edge-type filters.
 * Node kinds: claim · article · source · fact_check · person/organization/event · image · document · url
 * Edge types: supports · contradicts · references · published_by · mentions · derived_from · related_to
 *
 * STABILITY MODEL (v2)
 * The simulation is alpha-cooled: it starts at full energy, decays every tick,
 * and STOPS completely once settled (alpha <= alphaMin). Pan / zoom / selection
 * only redraw — they never re-heat the layout. Only dragging a node re-heats
 * locally, and it re-settles immediately after release. The initial layout is
 * seeded, so the same graph always produces the same arrangement.
 */
"use strict";

const EvidenceGraph = (() => {
  const KIND_COLORS = {
    claim: "#4a8cff", article: "#94a3b8", source: "#a371f7", fact_check: "#fbbf24",
    person: "#f472b6", organization: "#f472b6", event: "#fb923c",
    image: "#34d399", document: "#34d399", url: "#64748b", meta: "#334155",
  };
  const EDGE_COLORS = {
    supports: "rgba(52, 211, 153, 0.65)",
    contradicts: "rgba(248, 113, 113, 0.75)",
    references: "rgba(148, 163, 184, 0.3)",
    published_by: "rgba(163, 113, 247, 0.4)",
    mentions: "rgba(148, 163, 184, 0.25)",
    derived_from: "rgba(74, 140, 255, 0.35)",
    related_to: "rgba(251, 191, 36, 0.45)",
  };

  // Simulation tuning
  const ALPHA_START = 1.0;     // initial layout energy
  const ALPHA_MIN = 0.002;     // below this the simulation is frozen
  const ALPHA_DECAY = 0.03;    // per-tick cooling (1.0 -> 0.002 in ~170 ticks)
  const ALPHA_DRAG = 0.35;     // re-heat level while a node is being dragged
  const FRICTION = 0.62;       // velocity damping per tick
  const MAX_SPEED = 26;        // px/tick speed cap (prevents explosive kicks)
  const REPULSION = 2400;      // node-node repulsion strength
  const SPRING_K = 0.055;      // edge spring stiffness
  const GRAVITY = 0.028;       // pull toward canvas center
  const CLAIM_GRAVITY = 0.09;  // claims anchor harder at the center

  /* Deterministic PRNG (mulberry32) — same seed, same layout, every render. */
  function seededRng(seedStr) {
    let h = 2166136261 >>> 0;
    for (let i = 0; i < seedStr.length; i++) {
      h ^= seedStr.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    let a = h >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function render(container, graph) {
    const nodes = (graph.nodes || []).filter((n) => n.kind !== "meta");
    const edges = (graph.edges || []).filter((e) => e.source !== "nodes" && e.target !== "nodes");
    if (!nodes.length) {
      container.innerHTML = '<p style="color:var(--text-3);padding:20px;">No graph data.</p>';
      return;
    }

    const canvas = document.createElement("canvas");
    container.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    // ---- state
    const dpr = window.devicePixelRatio || 1;
    let W = 0, H = 0;
    const view = { x: 0, y: 0, scale: 1 };
    const activeEdges = new Set(Object.keys(EDGE_COLORS));
    const pos = new Map();
    const vel = new Map();
    let selected = null;
    let dragging = null;
    let panning = false;
    let lastMouse = null;
    let needsDraw = true;
    let alpha = ALPHA_START;

    /* Deterministic initial layout: claims near the center, sources /
       fact-checks on a mid ring, everything else on the outer ring. */
    const rng = seededRng(nodes.map((n) => n.key).join("|"));
    const ringKind = (k) => (k === "claim" ? 0 : (k === "source" || k === "fact_check") ? 1 : 2);
    const ringRadius = [36, 150, 260];
    const ringPos = new Map();
    nodes.forEach((n) => {
      const r = ringKind(n.kind);
      ringPos.set(r, (ringPos.get(r) || 0) + 1);
    });
    const ringIdx = new Map();
    nodes.forEach((n) => {
      const r = ringKind(n.kind);
      const i = (ringIdx.get(r) || 0);
      ringIdx.set(r, i + 1);
      const count = Math.max(1, ringPos.get(r));
      const angle = (i / count) * Math.PI * 2 + rng() * 0.6;
      const radius = ringRadius[r] + rng() * 34;
      pos.set(n.key, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
      vel.set(n.key, { x: 0, y: 0 });
    });

    function resize() {
      const rect = container.getBoundingClientRect();
      W = rect.width; H = rect.height;
      canvas.width = W * dpr; canvas.height = H * dpr;
      needsDraw = true; // redraw at the new size — layout is NOT re-heated
    }
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    // ---- force simulation (alpha-cooled; stops when settled)
    function tick() {
      const n2 = nodes.length;
      // pairwise repulsion (sparse O(n²) fine for our graph sizes)
      for (let i = 0; i < n2; i++) {
        const a = nodes[i], pa = pos.get(a.key);
        for (let j = i + 1; j < n2; j++) {
          const b = nodes[j], pb = pos.get(b.key);
          let dx = pa.x - pb.x, dy = pa.y - pb.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) {
            // deterministic separation via golden-angle direction
            const g = 2.399963 * (i * n2 + j);
            dx = Math.cos(g) * 0.5; dy = Math.sin(g) * 0.5; d2 = 1;
          }
          if (d2 > 250000) continue;
          const d = Math.sqrt(d2);
          let f = (REPULSION * alpha) / d2;
          if (f > 24) f = 24;
          const fx = (dx / d) * f, fy = (dy / d) * f;
          const va = vel.get(a.key), vb = vel.get(b.key);
          va.x += fx; va.y += fy;
          vb.x -= fx; vb.y -= fy;
        }
      }
      // edge springs
      for (const e of edges) {
        const pa = pos.get(e.source), pb = pos.get(e.target);
        if (!pa || !pb) continue;
        const dx = pb.x - pa.x, dy = pb.y - pa.y;
        const d = Math.max(1, Math.hypot(dx, dy));
        const rest = 92 + 26 * (e.weight || 1);
        const f = ((d - rest) / d) * SPRING_K * alpha;
        const fx = dx * f * 0.5, fy = dy * f * 0.5;
        const va = vel.get(e.source), vb = vel.get(e.target);
        if (va) { va.x += fx; va.y += fy; }
        if (vb) { vb.x -= fx; vb.y -= fy; }
      }
      // center gravity + integrate with friction
      for (const n of nodes) {
        const p = pos.get(n.key);
        const v = vel.get(n.key);
        if (n.key === dragging) { v.x = 0; v.y = 0; continue; } // pinned to cursor
        const g = (n.kind === "claim" ? CLAIM_GRAVITY : GRAVITY) * alpha;
        v.x += -p.x * g;
        v.y += -p.y * g;
        v.x *= FRICTION; v.y *= FRICTION;
        const sp = Math.hypot(v.x, v.y);
        if (sp > MAX_SPEED) { v.x = (v.x / sp) * MAX_SPEED; v.y = (v.y / sp) * MAX_SPEED; }
        p.x += v.x; p.y += v.y;
        p.x = Math.max(-W * 0.9, Math.min(W * 0.9, p.x));
        p.y = Math.max(-H * 0.9, Math.min(H * 0.9, p.y));
      }
      alpha += (0 - alpha) * ALPHA_DECAY; // cool down every tick
    }

    // ---- rendering
    function draw() {
      ctx.save();
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.scale(dpr, dpr);
      ctx.translate(W / 2 + view.x, H / 2 + view.y);
      ctx.scale(view.scale, view.scale);

      // edges
      edges.forEach((e) => {
        if (!activeEdges.has(e.edge_type)) return;
        const a = pos.get(e.source), b = pos.get(e.target);
        if (!a || !b) return;
        const dim = selected && e.source !== selected && e.target !== selected;
        ctx.strokeStyle = EDGE_COLORS[e.edge_type] || "rgba(148,163,184,0.3)";
        ctx.globalAlpha = dim ? 0.08 : (selected ? 1 : 0.9);
        ctx.lineWidth = e.edge_type === "contradicts" ? 2.2 : 1.2 + Math.min(1.5, (e.weight || 1) * 0.5);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      });

      // nodes
      nodes.forEach((n) => {
        const p = pos.get(n.key);
        const dim = selected && selected !== n.key &&
          !edges.some((e) => activeEdges.has(e.edge_type) &&
            ((e.source === selected && e.target === n.key) || (e.target === selected && e.source === n.key)));
        const r = n.kind === "claim" ? 15 : n.kind === "source" || n.kind === "fact_check" ? 11 : 8;
        ctx.globalAlpha = dim ? 0.18 : 1;

        if (selected === n.key) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, r + 6, 0, Math.PI * 2);
          ctx.strokeStyle = "rgba(74,140,255,0.65)";
          ctx.lineWidth = 2;
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fillStyle = KIND_COLORS[n.kind] || "#64748b";
        ctx.fill();
        if (n.kind === "claim") {
          ctx.strokeStyle = "rgba(230,237,247,0.8)";
          ctx.lineWidth = 1.4;
          ctx.stroke();
        }

        // labels (claims & sources always; others when zoomed in)
        if (n.kind === "claim" || n.kind === "source" || n.kind === "fact_check" || view.scale > 1.25) {
          ctx.font = `${n.kind === "claim" ? "600 " : ""}11px Inter, system-ui, sans-serif`;
          ctx.fillStyle = dim ? "rgba(230,237,247,0.25)" : "rgba(230,237,247,0.92)";
          ctx.textAlign = "center";
          const label = (n.label || n.key).slice(0, 34);
          ctx.fillText(label, p.x, p.y + r + 13);
        }
        ctx.globalAlpha = 1;
      });
      ctx.restore();
    }

    // ---- render loop: ticks only while there is energy, draws only on change
    function frame() {
      const energy = alpha > ALPHA_MIN || dragging;
      if (energy) {
        tick();
        needsDraw = true;
      }
      if (needsDraw) {
        draw();
        needsDraw = false;
      }
      requestAnimationFrame(frame);
    }

    // ---- interactions
    function toWorld(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (clientX - rect.left - W / 2 - view.x) / view.scale,
        y: (clientY - rect.top - H / 2 - view.y) / view.scale,
      };
    }
    function nodeAt(clientX, clientY) {
      const w = toWorld(clientX, clientY);
      for (const n of nodes) {
        const p = pos.get(n.key);
        const r = (n.kind === "claim" ? 16 : 11) + 4;
        if ((w.x - p.x) ** 2 + (w.y - p.y) ** 2 <= r * r) return n;
      }
      return null;
    }

    canvas.addEventListener("mousedown", (e) => {
      const hit = nodeAt(e.clientX, e.clientY);
      if (hit) {
        selected = selected === hit.key ? null : hit.key;
        dragging = hit.key;
        alpha = Math.max(alpha, ALPHA_DRAG); // mild local re-heat only
      } else {
        panning = true;
        selected = null;
        lastMouse = { x: e.clientX, y: e.clientY };
      }
      needsDraw = true;
    });
    canvas.addEventListener("mousemove", (e) => {
      if (dragging) {
        const w = toWorld(e.clientX, e.clientY);
        const p = pos.get(dragging);
        p.x = w.x; p.y = w.y;
        const v = vel.get(dragging);
        if (v) { v.x = 0; v.y = 0; }
        alpha = Math.max(alpha, ALPHA_DRAG);
        needsDraw = true;
      } else if (panning && lastMouse) {
        view.x += e.clientX - lastMouse.x;
        view.y += e.clientY - lastMouse.y;
        lastMouse = { x: e.clientX, y: e.clientY };
        needsDraw = true;
      }
    });
    window.addEventListener("mouseup", () => { dragging = null; panning = false; lastMouse = null; });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left - W / 2, my = e.clientY - rect.top - H / 2;
      view.x = mx - (mx - view.x) * factor;
      view.y = my - (my - view.y) * factor;
      view.scale = Math.max(0.35, Math.min(3.2, view.scale * factor));
      needsDraw = true; // zoom never re-heats the layout
    }, { passive: false });

    // touch (pan + pinch-lite)
    let touchDist = 0;
    canvas.addEventListener("touchstart", (e) => {
      if (e.touches.length === 1) {
        panning = true;
        lastMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      } else if (e.touches.length === 2) {
        touchDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      }
    }, { passive: true });
    canvas.addEventListener("touchmove", (e) => {
      e.preventDefault();
      if (e.touches.length === 1 && lastMouse) {
        view.x += e.touches[0].clientX - lastMouse.x;
        view.y += e.touches[0].clientY - lastMouse.y;
        lastMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      } else if (e.touches.length === 2) {
        const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
        view.scale = Math.max(0.35, Math.min(3.2, view.scale * (d / (touchDist || d))));
        touchDist = d;
      }
      needsDraw = true;
    }, { passive: false });
    canvas.addEventListener("touchend", () => { panning = false; lastMouse = null; });

    // ---- legend / edge filters (redraw only — layout untouched)
    const legend = document.createElement("div");
    legend.className = "graph-legend";
    const edgeTypes = [...new Set(edges.map((e) => e.edge_type))];
    legend.innerHTML = edgeTypes.map((t) =>
      `<button class="on" data-edge="${t}">${t.replace(/_/g, " ")}</button>`).join("");
    container.appendChild(legend);
    legend.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-edge]");
      if (!btn) return;
      const t = btn.dataset.edge;
      if (activeEdges.has(t)) { activeEdges.delete(t); btn.classList.remove("on"); }
      else { activeEdges.add(t); btn.classList.add("on"); }
      needsDraw = true;
    });

    const hint = document.createElement("div");
    hint.className = "graph-hint";
    hint.textContent = "drag nodes · scroll to zoom · click to isolate";
    container.appendChild(hint);

    frame();
  }

  return { render };
})();

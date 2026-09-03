/**
 * PR•VISION — Provider Sidecar
 * =============================
 * Local HTTP bridge between the Python backend and the z-ai-web-dev-sdk.
 *
 *   POST /web_search    { query, num?, recency_days? }  -> search results
 *   POST /page_reader   { url }                        -> extracted page (title/html/publishedTime)
 *   POST /llm           { system?, messages | prompt } -> model text reply
 *   POST /vision        { text, image_url }            -> multimodal model text reply
 *   POST /image_search  { query, count? }              -> web image results
 *   GET  /health                                       -> provider capability probe
 *
 * Design rules:
 *  - Bind to 127.0.0.1 ONLY (never expose externally; the backend is the sole client).
 *  - Every response is JSON: { ok: true, data } | { ok: false, error }.
 *  - Hard per-request timeout + concurrency cap so the backend never hangs.
 *  - The SDK client is created lazily once and reused.
 *
 * Start:  node server.mjs   (or: bun server.mjs)
 * Env:    SIDECAR_PORT (default 8787), SIDECAR_TIMEOUT_MS (default 60000)
 */
import http from "node:http";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// Resolve the SDK: local node_modules first (project delivery), then the bun
// global store (sandbox). ESM import of the package's CJS/ESM entry via createRequire.
let ZAI = null;
let sdkError = null;
try {
  ZAI = (require("z-ai-web-dev-sdk"), null);
} catch { /* fall through to absolute path */ }
try {
  // eslint-disable-next-line no-constant-condition
  if (!ZAI) {
    const mod = await import("z-ai-web-dev-sdk").catch(async () => {
      const globalPath = "/home/z/.bun/install/global/node_modules/z-ai-web-dev-sdk/dist/index.js";
      return import(globalPath);
    });
    ZAI = mod.default ?? mod;
  }
} catch (err) {
  sdkError = String(err?.message ?? err);
}

const PORT = Number(process.env.SIDECAR_PORT ?? 8787);
const TIMEOUT_MS = Number(process.env.SIDECAR_TIMEOUT_MS ?? 60000);
const MAX_CONCURRENCY = Number(process.env.SIDECAR_MAX_CONCURRENCY ?? 4);

let clientPromise = null;
let inFlight = 0;

function getClient() {
  if (!ZAI) return Promise.reject(new Error(`sdk_unavailable: ${sdkError ?? "not installed"}`));
  if (!clientPromise) clientPromise = ZAI.create();
  return clientPromise;
}

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${label}_timeout_after_${ms}ms`)), ms);
    promise.then(
      (v) => { clearTimeout(t); resolve(v); },
      (e) => { clearTimeout(t); reject(e); },
    );
  });
}

function send(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 30 * 1024 * 1024) { reject(new Error("payload_too_large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {}); }
      catch (e) { reject(new Error(`invalid_json: ${e.message}`)); }
    });
    req.on("error", reject);
  });
}

/** Run a provider call with concurrency guard + timeout. */
async function guarded(res, label, fn) {
  if (inFlight >= MAX_CONCURRENCY) {
    return send(res, 429, { ok: false, error: "sidecar_busy" });
  }
  inFlight += 1;
  try {
    const data = await withTimeout(fn(), TIMEOUT_MS, label);
    send(res, 200, { ok: true, data });
  } catch (err) {
    send(res, 200, { ok: false, error: String(err?.message ?? err).slice(0, 500) });
  } finally {
    inFlight -= 1;
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (req.method === "GET" && url.pathname === "/health") {
    let sdkReady = false;
    try { await withTimeout(getClient(), 8000, "health"); sdkReady = true; } catch { sdkReady = false; }
    return send(res, 200, {
      ok: true,
      data: {
        status: sdkReady ? "healthy" : "degraded",
        sdk_ready: sdkReady,
        sdk_error: sdkReady ? null : sdkError,
        in_flight: inFlight,
        capabilities: {
          web_search: sdkReady, page_reader: sdkReady, llm: sdkReady,
          vision: sdkReady, image_search: sdkReady,
        },
      },
    });
  }

  if (req.method !== "POST") return send(res, 404, { ok: false, error: "not_found" });

  let body;
  try { body = await readBody(req); }
  catch (e) { return send(res, 400, { ok: false, error: e.message }); }

  try {
    switch (url.pathname) {
      case "/web_search": {
        const query = String(body.query ?? "").trim();
        if (!query) return send(res, 400, { ok: false, error: "query_required" });
        return guarded(res, "web_search", async () => {
          const zai = await getClient();
          const args = { query, num: Math.min(Number(body.num ?? 8) || 8, 20) };
          if (body.recency_days) args.recency_days = Number(body.recency_days);
          const results = await zai.functions.invoke("web_search", args);
          return (results ?? []).map((r) => ({
            url: r.url, name: r.name, snippet: r.snippet,
            host: r.host_name, rank: r.rank, date: r.date ?? null,
            source: "zai_web_search",
          }));
        });
      }

      case "/page_reader": {
        const target = String(body.url ?? "").trim();
        if (!/^https?:\/\//i.test(target)) return send(res, 400, { ok: false, error: "http_url_required" });
        return guarded(res, "page_reader", async () => {
          const zai = await getClient();
          const result = await zai.functions.invoke("page_reader", { url: target });
          const d = result?.data ?? {};
          return {
            url: d.url ?? target,
            title: d.title ?? null,
            html: d.html ?? "",
            published_time: d.publishedTime ?? null,
            source: "zai_page_reader",
          };
        });
      }

      case "/llm": {
        return guarded(res, "llm", async () => {
          const zai = await getClient();
          const messages = Array.isArray(body.messages) && body.messages.length
            ? body.messages
            : [
                ...(body.system ? [{ role: "system", content: String(body.system) }] : []),
                { role: "user", content: String(body.prompt ?? "") },
              ];
          const completion = await zai.chat.completions.create({
            messages,
            thinking: { type: "disabled" },
            ...(body.max_tokens ? { max_tokens: Number(body.max_tokens) } : {}),
          });
          return {
            text: completion?.choices?.[0]?.message?.content ?? "",
            model: completion?.model ?? null,
            source: "zai_llm",
          };
        });
      }

      case "/vision": {
        const imageUrl = String(body.image_url ?? "");
        if (!imageUrl) return send(res, 400, { ok: false, error: "image_url_required" });
        return guarded(res, "vision", async () => {
          const zai = await getClient();
          const completion = await zai.chat.completions.createVision({
            model: body.model ?? undefined,
            messages: [{
              role: "user",
              content: [
                { type: "text", text: String(body.text ?? "Describe this image.") },
                { type: "image_url", image_url: { url: imageUrl } },
              ],
            }],
            thinking: { type: "disabled" },
          });
          return {
            text: completion?.choices?.[0]?.message?.content ?? "",
            model: completion?.model ?? null,
            source: "zai_vision",
          };
        });
      }

      case "/image_search": {
        const query = String(body.query ?? "").trim();
        if (!query) return send(res, 400, { ok: false, error: "query_required" });
        return guarded(res, "image_search", async () => {
          const zai = await getClient();
          const result = await zai.images.search.create({
            query, count: Math.min(Number(body.count ?? 8) || 8, 20),
          });
          return {
            results: (result?.results ?? []).map((r) => ({
              original_url: r.original_url, caption: r.caption ?? null,
              source: r.source ?? null, width: r.original_width ?? null, height: r.original_height ?? null,
            })),
            provider: "zai_image_search",
          };
        });
      }

      default:
        return send(res, 404, { ok: false, error: "unknown_endpoint" });
    }
  } catch (err) {
    return send(res, 500, { ok: false, error: String(err?.message ?? err).slice(0, 500) });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[sidecar] PR•VISION provider sidecar on http://127.0.0.1:${PORT} (sdk=${ZAI ? "loaded" : "MISSING"})`);
});

#!/bin/sh
# =============================================================================
# PR•VISION container entrypoint
# 1. Materialize the z-ai SDK config when provided via ZAI_CONFIG_JSON.
# 2. Auto-start the embedded provider sidecar (Node is baked into the image)
#    unless SIDECAR_EMBEDDED=false — used when compose runs sidecar as a
#    separate service instead.
# 3. Exec the main command (uvicorn CMD or docker-compose command override).
# =============================================================================
set -e

if [ -n "${ZAI_CONFIG_JSON:-}" ] && [ ! -f "${HOME}/.z-ai-config" ]; then
  printf '%s' "$ZAI_CONFIG_JSON" > "${HOME}/.z-ai-config" 2>/dev/null || true
  echo "[entrypoint] wrote ${HOME}/.z-ai-config from ZAI_CONFIG_JSON"
fi

if [ "${SIDECAR_EMBEDDED:-true}" = "true" ] \
   && command -v node >/dev/null 2>&1 \
   && [ -f /srv/sidecar/server.mjs ]; then
  if ! curl -fsS --max-time 2 "http://127.0.0.1:${SIDECAR_PORT:-8787}/health" >/dev/null 2>&1; then
    ( cd /srv/sidecar && nohup node server.mjs > /tmp/prvision-sidecar.log 2>&1 & ) || true
    echo "[entrypoint] embedded provider sidecar starting on 127.0.0.1:${SIDECAR_PORT:-8787}"
  fi
fi

exec "$@"

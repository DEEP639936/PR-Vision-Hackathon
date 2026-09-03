#!/bin/sh
# =============================================================================
# PR•VISION sidecar entrypoint
# Materializes the z-ai SDK config from env when provided, then execs CMD.
#
# The z-ai-web-dev-sdk looks for a `.z-ai-config` JSON file in the process
# cwd, $HOME, or /etc. Inside the container the simplest injection is the
# ZAI_CONFIG_JSON env var (see docker-compose.yml / DEPLOYMENT.md).
# =============================================================================
set -e

if [ -n "${ZAI_CONFIG_JSON:-}" ] && [ ! -f "${HOME}/.z-ai-config" ]; then
  printf '%s' "$ZAI_CONFIG_JSON" > "${HOME}/.z-ai-config"
  echo "[sidecar-entrypoint] wrote ${HOME}/.z-ai-config from ZAI_CONFIG_JSON"
fi

exec "$@"

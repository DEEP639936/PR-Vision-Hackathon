# =============================================================================
# PR•VISION — Provider Sidecar container
# Node 20 slim, non-root, internal-only HTTP bridge to z-ai-web-dev-sdk
# (web search / page reader / LLM / vision / image search).
#
# SDK configuration (pick ONE):
#   1. Set ZAI_CONFIG_JSON env var  -> full JSON of the .z-ai-config file,
#      materialized to $HOME/.z-ai-config by the entrypoint at start.
#   2. Mount the file directly:
#        volumes:
#          - ./.z-ai-config:/home/node/.z-ai-config:ro
#
# Start:  docker build -t prvision-sidecar ./sidecar
#         docker run -p 127.0.0.1:8787:8787 prvision-sidecar
# =============================================================================
FROM node:20-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    NODE_ENV=production \
    SIDECAR_PORT=8787

WORKDIR /srv/sidecar

# Dependencies first for layer caching
COPY package.json server.mjs docker-entrypoint.sh ./
RUN npm install --omit=dev --no-audit --no-fund \
 && npm cache clean --force \
 && mkdir -p /home/node \
 && chown -R node:node /srv/sidecar /home/node \
 && chmod +x docker-entrypoint.sh \
 && mv docker-entrypoint.sh /usr/local/bin/docker-entrypoint

USER node

EXPOSE 8787

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["node", "server.mjs"]

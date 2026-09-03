# =============================================================================
# PR•VISION — production container (single image: FastAPI API + ML + sidecar)
# - Python 3.12 slim runtime, non-root user, healthcheck
# - Node 20 runtime + provider sidecar baked in (real-platform harvest,
#   web-search / page-reader / LLM / vision evidence) — disable per deploy
#   with SIDECAR_EMBEDDED=false and point SIDECAR_URL at a standalone sidecar
# - Requirements + sidecar deps copied before app code for layer caching
# =============================================================================
FROM node:20-slim AS sidecar-deps
WORKDIR /srv/sidecar
COPY sidecar/package.json sidecar/server.mjs ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/prvision

# Build deps for PyMySQL/cryptography + curl (healthcheck) + libstdc++ (node).
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-libmysqlclient-dev \
        curl \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first for layer caching
COPY backend/requirements.txt /srv/prvision/backend/requirements.txt
RUN pip install --no-cache-dir -r /srv/prvision/backend/requirements.txt

# Application code
COPY backend /srv/prvision/backend
COPY frontend /srv/prvision/frontend
COPY scripts /srv/prvision/scripts

# OCR engine for image/document text extraction (spec #8); tesseract is the
# runtime binary used by pytesseract. Poppler not required (PyMuPDF parses PDFs).
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Node runtime + pre-installed sidecar (embedded mode).
COPY --from=sidecar-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=sidecar-deps /srv/sidecar /srv/sidecar
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint

# Model + dataset directories (mounted as volumes in compose)
RUN mkdir -p /srv/prvision/ml/models /srv/prvision/ml/datasets /srv/prvision/ml/uploads \
 && chmod +x /usr/local/bin/docker-entrypoint \
 && useradd --create-home --uid 1001 prvision \
 && chown -R prvision:prvision /srv/prvision /srv/sidecar

# Sidecar defaults (embedded mode; override per environment)
ENV SIDECAR_EMBEDDED=true \
    SIDECAR_PORT=8787 \
    SIDECAR_URL=http://127.0.0.1:8787

USER prvision
WORKDIR /srv/prvision/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# Alembic migrations + first-run training run via the command override in
# docker-compose.yml / docker-compose.lite.yml.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]

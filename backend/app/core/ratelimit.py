"""In-process sliding-window rate limiter (spec #17).

Per-IP buckets with separate budgets for auth, verification, export and the
general API. Disabled via RATE_LIMIT_ENABLED=false (tests / trusted deploys).
No external dependency; single-process only (documented limitation: run a
shared limiter — e.g. Redis — when scaling beyond one API process).
"""
from __future__ import annotations

import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.security.ratelimit")

_EXEMPT_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


def _bucket_for(path: str, method: str = "GET") -> tuple[str, int]:
    if path.startswith("/api/auth"):
        return "auth", settings.RATE_LIMIT_AUTH_PER_MINUTE
    if path.startswith("/api/verify") and not path.startswith("/api/verify/jobs"):
        # Only SUBMISSIONS are expensive (fan-out evidence retrieval) — status
        # polls and report fetches must NOT burn the submission budget, or the
        # Verify page's own progress polling would 429 mid-job.
        if method == "POST":
            return "verify", settings.RATE_LIMIT_VERIFY_PER_MINUTE
        return "api", settings.RATE_LIMIT_API_PER_MINUTE
    if "/export" in path:
        return "export", settings.RATE_LIMIT_EXPORT_PER_MINUTE
    if path.startswith("/api/"):
        return "api", settings.RATE_LIMIT_API_PER_MINUTE
    return "pages", 600


class SlidingWindow:
    __slots__ = ("hits",)

    def __init__(self) -> None:
        self.hits: deque[float] = deque()


class RateLimitMiddleware(BaseHTTPMiddleware):
    WINDOW_SECONDS = 60.0

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self._buckets: dict[str, SlidingWindow] = {}
        self._lock = threading.Lock()

    async def dispatch(self, request, call_next):  # noqa: ANN001, ANN202
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path in _EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        name, limit = _bucket_for(path, request.method)
        client_ip = request.client.host if request.client else "unknown"
        key = f"{name}:{client_ip}"
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS

        with self._lock:
            window = self._buckets.setdefault(key, SlidingWindow())
            while window.hits and window.hits[0] < cutoff:
                window.hits.popleft()
            if len(window.hits) >= limit:
                retry_after = max(1, int(self.WINDOW_SECONDS - (now - window.hits[0])))
                logger.warning("Rate limit hit: %s %s (%s/%s in window)", name, path,
                               len(window.hits), limit)
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded ({limit}/min for {name}). "
                                       "Retry shortly."},
                    headers={"Retry-After": str(retry_after)},
                )
            window.hits.append(now)
            # opportunistic cleanup of dead buckets
            if len(self._buckets) > 10_000:
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if v.hits and v.hits[-1] >= cutoff}

        response = await call_next(request)
        if name != "pages":
            response.headers.setdefault("X-RateLimit-Limit", str(limit))
        return response

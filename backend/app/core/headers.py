"""Security response headers (spec #17) — applied to every response."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "          # inline boot snippets in vanilla pages
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-XSS-Protection", "0")  # modern: rely on CSP + escaping
        return response

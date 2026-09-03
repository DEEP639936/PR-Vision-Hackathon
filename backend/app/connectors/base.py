"""Platform connector abstraction.

Every connector (real or demo) implements the same async interface and emits
*normalised* payloads so downstream ingestion/feature/ML code never needs to
know which platform data came from.

Design rules (enforced by code review, not just convention):
1. Only OFFICIAL platform APIs are called. No scraping, no invented endpoints.
2. A metric a platform does not expose is `None` — never 0, never invented.
3. Connectors are stateless regarding DB; they return plain dicts.
4. Failures raise ConnectorError; the ingestion service isolates them per
   platform so one broken connector never crashes another.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

from app.core.logging import get_logger

logger = get_logger("prvision.connectors")

SUPPORTED_PLATFORMS = (
    "demo", "x", "reddit", "instagram", "facebook", "linkedin",
    "mastodon", "hackernews",
)

# Platforms whose public posts are harvested through the provider sidecar's
# web search (keyless, REAL posts) when official API credentials are absent.
HARVEST_PLATFORMS = ("x", "reddit", "instagram", "facebook", "linkedin")

# Per-platform .env credential hints used in honest status detail text.
OFFICIAL_API_KEY_HINTS = {
    "x": "X_BEARER_TOKEN",
    "reddit": "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET",
    "instagram": "META_ACCESS_TOKEN + META_INSTAGRAM_ACCOUNT_ID",
    "facebook": "META_ACCESS_TOKEN + META_PAGE_ID",
    "linkedin": "LINKEDIN_ACCESS_TOKEN",
}


class ConnectorError(Exception):
    """Raised when a platform fetch fails after retries.

    `kind` carries the failure category so the ingestion service can surface
    honest platform states ("rate_limited" → RATE_LIMITED on /api/platforms).
    """

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class RateLimitedError(ConnectorError):
    """Raised on HTTP 429 after honoring Retry-Backoff (spec #12)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, kind="rate_limited")
        self.retry_after = retry_after


class NotConfiguredError(ConnectorError):
    """Raised when a connector has no credentials configured."""


@dataclass
class ConnectorStatus:
    """Result of a connector health check — reflects REAL state only."""

    platform: str
    configured: bool
    healthy: bool = False
    detail: str = ""
    latency_ms: float | None = None


@dataclass
class NormalizedPost:
    """The common PR•VISION post format (all connectors emit this)."""

    platform: str
    post_id: str
    author_id: str
    content: str
    posted_at: datetime
    url: str | None = None
    language: str | None = None
    author_display_name: str | None = None
    is_demo: bool = False
    # metrics at fetch time — None means "platform does not expose this"
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    followers: int | None = None
    unique_sharers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "post_id": self.post_id,
            "author_id": self.author_id,
            "content": self.content,
            "posted_at": self.posted_at,
            "url": self.url,
            "language": self.language,
            "author_display_name": self.author_display_name,
            "is_demo": self.is_demo,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "views": self.views,
            "followers": self.followers,
            "unique_sharers": self.unique_sharers,
        }


@dataclass
class NormalizedMetrics:
    """Metric snapshot at a point in time."""

    timestamp: datetime
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None
    followers: int | None = None
    unique_sharers: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "views": self.views,
            "followers": self.followers,
            "unique_sharers": self.unique_sharers,
        }


@dataclass
class NormalizedPropagationEvent:
    """One reshare edge: `source_user_id` reshared to/through `target_user_id`."""

    source_user_id: str | None
    target_user_id: str | None
    event_type: str  # share | repost | quote | crosspost
    timestamp: datetime
    time_since_original_post: float | None = None  # seconds
    depth: int | None = None  # 0 = origin post, 1 = direct reshare, 2 = reshare of reshare

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_user_id": self.source_user_id,
            "target_user_id": self.target_user_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "time_since_original_post": self.time_since_original_post,
            "depth": self.depth,
        }


class SocialPlatformConnector(abc.ABC):
    """Common async interface implemented by every platform adapter."""

    platform: str = "abstract"
    # Discovery: connector can surface NEW public posts each cycle (harvesters,
    # public-API connectors). The ingestion scheduler polls it, throttled by
    # discovery_interval_seconds; plain official-API connectors keep discovery
    # off (their fetch_posts is a bootstrap/maintenance concern).
    supports_discovery: bool = False
    # Minimum seconds between metric-refresh passes (0 = every cycle). Public
    # API connectors use this to stay polite to instances/endpoints.
    min_poll_seconds: float = 0.0

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None
        self._last_metric_poll: float = 0.0
        self._last_discovery: float = 0.0

    # ------------------------------------------------------------------ api
    @abc.abstractmethod
    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        """Fetch recent posts to begin monitoring."""

    @abc.abstractmethod
    async def fetch_post_metrics(
        self,
        post_id: str,
        *,
        since: datetime | None = None,
        post_posted_at: datetime | None = None,
    ) -> list[NormalizedMetrics]:
        """Fetch current metrics for a monitored post (new snapshots).

        `post_posted_at` lets stateless connectors (demo) continue their
        deterministic timeline; real API connectors ignore it.
        """

    @abc.abstractmethod
    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        """Fetch reshare/propagation edges (empty where platform hides them)."""

    @abc.abstractmethod
    async def health_check(self) -> ConnectorStatus:
        """Report configuration + reachability. Must never lie."""

    # -------------------------------------------------------------- helpers
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _rate_limit_headers(response: httpx.Response) -> dict[str, str]:
        keys = ("x-rate-limit-remaining", "x-rate-limit-reset", "retry-after")
        return {k: response.headers[k] for k in keys if k in response.headers}

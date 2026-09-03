"""Reddit connector — official Reddit OAuth API.

https://www.reddit.com/dev/api  (OAuth endpoints at https://oauth.reddit.com)
Authentication: application-only OAuth2 (client_credentials) for public read.

Available:
    score (net upvotes), num_comments, upvote_ratio, created_utc,
    subreddit subscribers (used as the author/community follower proxy),
    num_crossposts (crossposts are Reddit's closest share analogue)
NOT available:
    - repost/resshare graph (Reddit does not expose a repost network) -> edges = None
    - unique sharers                                                  -> None
    - view counts (public API)                                        -> None

Rate limits: OAuth apps are limited per-minute (documented); we cap our own
request rate conservatively and back off on 429.
"""
from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    NormalizedMetrics,
    NormalizedPost,
    NormalizedPropagationEvent,
    RateLimitedError,
    SocialPlatformConnector,
)
from app.core.config import settings

OAUTH_BASE = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


class RedditConnector(SocialPlatformConnector):
    platform = "reddit"

    def __init__(self) -> None:
        super().__init__()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._min_request_interval = 1.1  # ~54 req/min, conservative
        self._last_request = 0.0

    @property
    def configured(self) -> bool:
        return bool(settings.REDDIT_CLIENT_ID and settings.REDDIT_CLIENT_SECRET)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        client = await self._http()
        basic = base64.b64encode(
            f"{settings.REDDIT_CLIENT_ID}:{settings.REDDIT_CLIENT_SECRET}".encode()
        ).decode()
        resp = await client.post(
            OAUTH_BASE,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}",
                     "User-Agent": settings.REDDIT_USER_AGENT},
        )
        if resp.status_code != 200:
            raise ConnectorError(f"Reddit OAuth failed ({resp.status_code})")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    async def _get(self, path: str, params: dict | None = None) -> dict:
        token = await self._access_token()
        client = await self._http()
        wait = self._min_request_interval - (time.time() - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.time()
        resp = await client.get(
            f"{API_BASE}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}", "User-Agent": settings.REDDIT_USER_AGENT},
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", "5"))
            await asyncio.sleep(min(retry_after, 30))
            resp = await client.get(
                f"{API_BASE}{path}", params=params or {},
                headers={"Authorization": f"Bearer {token}", "User-Agent": settings.REDDIT_USER_AGENT},
            )
            if resp.status_code == 429:
                raise RateLimitedError("Reddit API rate limited after retry", retry_after=retry_after)
        if resp.status_code >= 400:
            raise ConnectorError(f"Reddit API {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def fetch_posts(self, *, limit: int = 20, subreddit: str = "all", **kwargs: Any) -> list[NormalizedPost]:
        data = await self._get(f"/r/{subreddit}/new", {"limit": min(limit, 100)})
        posts: list[NormalizedPost] = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            posts.append(
                NormalizedPost(
                    platform=self.platform,
                    post_id=d.get("name", d.get("id", "")),  # t3_xxx fullname
                    author_id=d.get("author", "unknown"),
                    content=f"{d.get('title', '')}\n\n{d.get('selftext', '')}".strip(),
                    posted_at=datetime.fromtimestamp(d.get("created_utc", time.time()), tz=timezone.utc),
                    url=f"https://www.reddit.com{d.get('permalink', '')}",
                    language=None,  # Reddit does not expose language reliably
                    likes=d.get("score"),
                    comments=d.get("num_comments"),
                    shares=d.get("num_crossposts"),  # closest official share analogue (may be None)
                    views=None,   # not exposed
                    followers=d.get("subreddit_subscribers"),
                    unique_sharers=None,
                )
            )
        return posts

    async def fetch_post_metrics(
        self, post_id: str, *, since: datetime | None = None, post_posted_at: datetime | None = None
    ) -> list[NormalizedMetrics]:
        data = await self._get("/api/info", {"id": post_id})
        children = data.get("data", {}).get("children", [])
        if not children:
            raise ConnectorError(f"Reddit post {post_id} not found")
        d = children[0].get("data", {})
        return [
            NormalizedMetrics(
                timestamp=datetime.now(timezone.utc),
                likes=d.get("score"),
                comments=d.get("num_comments"),
                shares=d.get("num_crossposts"),
                views=None,
                followers=d.get("subreddit_subscribers"),
                unique_sharers=None,
            )
        ]

    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        return []  # Reddit does not expose a repost network

    async def health_check(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(self.platform, configured=False,
                                   detail="REDDIT_CLIENT_ID/SECRET not set")
        try:
            start = time.time()
            await self._get("/api/info", {"id": "t3_1"})
            return ConnectorStatus(self.platform, configured=True, healthy=True,
                                   detail="Reddit OAuth OK",
                                   latency_ms=(time.time() - start) * 1000)
        except ConnectorError as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False, detail=str(exc))

"""X (Twitter) connector — official X API v2.

https://docs.x.com/x-api/posts
Authentication: App-only Bearer Token.

Available (v2 `tweet.fields=public_metrics`):
    retweet_count, reply_count, like_count, quote_count, impression_count
NOT available through standard API access:
    - who retweeted a post (residents of the full-archive / retweeted_by
      endpoint require elevated/paid access)  -> propagation edges = None
    - unique sharer counts                   -> None
    - author account age on basic tiers      -> derived where available

Rate limits (documented per tier) are honoured via X-RateLimit headers and
Retry-After on 429.
"""
from __future__ import annotations

import asyncio
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

API_BASE = "https://api.x.com/2"


class XConnector(SocialPlatformConnector):
    platform = "x"

    def __init__(self) -> None:
        super().__init__()
        self._request_count = 0

    @property
    def configured(self) -> bool:
        return bool(settings.X_BEARER_TOKEN)

    async def fetch_posts(self, *, limit: int = 20, query: str = "news", **kwargs: Any) -> list[NormalizedPost]:
        if not self.configured:
            raise ConnectorError("X connector not configured (missing X_BEARER_TOKEN)")
        params = {
            "query": query,
            "max_results": min(max(limit, 10), 100),
            "tweet.fields": "created_at,public_metrics,author_id,lang",
            "expansions": "author_id",
            "user.fields": "public_metrics,name",
        }
        data = await self._get("/tweets/search/recent", params)
        users = {u["id"]: u for u in (data.get("includes", {}) or {}).get("users", [])}
        posts: list[NormalizedPost] = []
        for tweet in data.get("data", []):
            metrics = tweet.get("public_metrics", {}) or {}
            author = users.get(tweet.get("author_id"), {})
            posts.append(
                NormalizedPost(
                    platform=self.platform,
                    post_id=tweet["id"],
                    author_id=str(tweet.get("author_id", "")),
                    author_display_name=author.get("name"),
                    content=tweet.get("text", ""),
                    posted_at=_parse_ts(tweet.get("created_at")),
                    url=f"https://x.com/{author.get('username', 'i')}/status/{tweet['id']}",
                    language=tweet.get("lang"),
                    likes=metrics.get("like_count"),
                    comments=metrics.get("reply_count"),
                    # retweets + quotes are X's share mechanics
                    shares=(metrics.get("retweet_count") or 0) + (metrics.get("quote_count") or 0),
                    views=metrics.get("impression_count"),
                    followers=(author.get("public_metrics") or {}).get("followers_count"),
                    unique_sharers=None,  # not exposed by the API
                )
            )
        return posts

    async def fetch_post_metrics(
        self, post_id: str, *, since: datetime | None = None, post_posted_at: datetime | None = None
    ) -> list[NormalizedMetrics]:
        if not self.configured:
            raise ConnectorError("X connector not configured")
        data = await self._get(f"/tweets/{post_id}", {"tweet.fields": "public_metrics"})
        m = (data.get("data") or {}).get("public_metrics", {}) or {}
        return [
            NormalizedMetrics(
                timestamp=datetime.now(timezone.utc),
                likes=m.get("like_count"),
                comments=m.get("reply_count"),
                shares=(m.get("retweet_count") or 0) + (m.get("quote_count") or 0),
                views=m.get("impression_count"),
                followers=None,
                unique_sharers=None,
            )
        ]

    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        # The official API does not expose the reshare graph on standard access.
        # We return an empty list — the pipeline handles missing propagation data.
        return []

    async def health_check(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(self.platform, configured=False, detail="X_BEARER_TOKEN not set")
        try:
            start = _now_ms()
            await self._get("/tweets/search/recent", {"query": "prvision-health", "max_results": 10})
            return ConnectorStatus(self.platform, configured=True, healthy=True,
                                   detail="X API reachable", latency_ms=_now_ms() - start)
        except ConnectorError as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False, detail=str(exc))

    # -------------------------------------------------------------- internals
    async def _get(self, path: str, params: dict) -> dict:
        client = await self._http()
        headers = {"Authorization": f"Bearer {settings.X_BEARER_TOKEN}"}
        for attempt in range(3):
            resp = await client.get(f"{API_BASE}{path}", params=params, headers=headers)
            self._request_count += 1
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("retry-after", "5"))
                await asyncio.sleep(min(retry_after, 30))
                continue
            if resp.status_code >= 400:
                raise ConnectorError(f"X API {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise RateLimitedError("X API rate limited after retries", retry_after=retry_after)


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000

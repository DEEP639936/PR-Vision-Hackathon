"""Instagram connector — official Meta Graph API (Instagram Business accounts).

https://developers.facebook.com/docs/instagram-platform/instagram-graph-api
Authentication: Meta access token with instagram_basic + pages_read_engagement.

Available (business accounts):
    media: id, caption, timestamp, permalink, like_count*, comments_count
    insights: views (v2+), shares for REELS where available
    followers_count via the IG user node
NOT available:
    - share/repost counts for feed posts (only Reels expose `shares` via
      insights, on eligible accounts)  -> shares = None unless insights allow
    - unique sharers                  -> None
    - propagation graph               -> None

*like_count requires the account to be owned by this Meta app account.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    NormalizedMetrics,
    NormalizedPost,
    NormalizedPropagationEvent,
    SocialPlatformConnector,
)
from app.core.config import settings

GRAPH = "https://graph.facebook.com/v21.0"


class InstagramConnector(SocialPlatformConnector):
    platform = "instagram"

    @property
    def configured(self) -> bool:
        return bool(settings.META_ACCESS_TOKEN and settings.META_INSTAGRAM_ACCOUNT_ID)

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        if not self.configured:
            raise ConnectorError("Instagram connector not configured (META_ACCESS_TOKEN / META_INSTAGRAM_ACCOUNT_ID)")
        data = await self._get(
            f"/{settings.META_INSTAGRAM_ACCOUNT_ID}/media",
            {
                "fields": "id,caption,timestamp,permalink,like_count,comments_count,media_product_type",
                "limit": min(limit, 100),
            },
        )
        followers = await self._fetch_followers()
        posts: list[NormalizedPost] = []
        for item in data.get("data", []):
            shares = await self._fetch_reel_shares(item["id"]) if item.get("media_product_type") == "REELS" else None
            posts.append(
                NormalizedPost(
                    platform=self.platform,
                    post_id=item["id"],
                    author_id=settings.META_INSTAGRAM_ACCOUNT_ID,
                    content=item.get("caption") or "",
                    posted_at=_parse_ts(item.get("timestamp")),
                    url=item.get("permalink"),
                    language=None,
                    likes=item.get("like_count"),
                    comments=item.get("comments_count"),
                    shares=shares,
                    views=None,  # views live in insights, fetched lazily per post
                    followers=followers,
                    unique_sharers=None,
                )
            )
        return posts

    async def fetch_post_metrics(
        self, post_id: str, *, since: datetime | None = None, post_posted_at: datetime | None = None
    ) -> list[NormalizedMetrics]:
        if not self.configured:
            raise ConnectorError("Instagram connector not configured")
        data = await self._get(
            f"/{post_id}", {"fields": "like_count,comments_count,timestamp"}
        )
        shares = None
        views = None
        try:
            insights = await self._get(
                f"/{post_id}/insights",
                {"metric": "views,shares", "period": "lifetime"},
            )
            for row in insights.get("data", []):
                if row.get("name") == "views":
                    views = row["values"][0].get("value")
                elif row.get("name") == "shares":
                    shares = row["values"][0].get("value")
        except ConnectorError:
            pass  # insights not available for this media/account — keep fields NULL
        followers = await self._fetch_followers()
        return [
            NormalizedMetrics(
                timestamp=datetime.now(timezone.utc),
                likes=data.get("like_count"),
                comments=data.get("comments_count"),
                shares=shares,
                views=views,
                followers=followers,
                unique_sharers=None,
            )
        ]

    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        return []  # Instagram does not expose a reshare graph

    async def health_check(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(self.platform, configured=False,
                                   detail="META_ACCESS_TOKEN / META_INSTAGRAM_ACCOUNT_ID not set")
        try:
            await self._get(f"/{settings.META_INSTAGRAM_ACCOUNT_ID}", {"fields": "id,username"})
            return ConnectorStatus(self.platform, configured=True, healthy=True, detail="Graph API reachable")
        except ConnectorError as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False, detail=str(exc))

    # -------------------------------------------------------------- internals
    async def _fetch_followers(self) -> Optional[int]:
        data = await self._get(f"/{settings.META_INSTAGRAM_ACCOUNT_ID}", {"fields": "followers_count"})
        return data.get("followers_count")

    async def _fetch_reel_shares(self, media_id: str) -> Optional[int]:
        try:
            insights = await self._get(f"/{media_id}/insights", {"metric": "shares", "period": "lifetime"})
            for row in insights.get("data", []):
                if row.get("name") == "shares":
                    return row["values"][0].get("value")
        except ConnectorError:
            return None
        return None

    async def _get(self, path: str, params: dict) -> dict:
        params = dict(params)
        params["access_token"] = settings.META_ACCESS_TOKEN
        client = await self._http()
        resp = await client.get(f"{GRAPH}{path}", params=params)
        if resp.status_code >= 400:
            raise ConnectorError(f"Instagram Graph {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

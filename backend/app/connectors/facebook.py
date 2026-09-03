"""Facebook connector — official Meta Graph API (Pages).

https://developers.facebook.com/docs/pages-api
Authentication: Page or User access token with pages_read_engagement.

Available:
    posts: created_time, message, permalink_url, shares.count,
           comments.summary(true).limit(0), likes.summary(true).limit(0)
    fan_count on the Page node
NOT available:
    - per-post view counts (page-level insights only) -> views = None
    - unique sharers                                  -> None
    - propagation graph                               -> None
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

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


class FacebookConnector(SocialPlatformConnector):
    platform = "facebook"

    @property
    def configured(self) -> bool:
        return bool(settings.META_ACCESS_TOKEN and settings.META_PAGE_ID)

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        if not self.configured:
            raise ConnectorError("Facebook connector not configured (META_ACCESS_TOKEN / META_PAGE_ID)")
        page = await self._get(f"/{settings.META_PAGE_ID}", {"fields": "fan_count,name"})
        data = await self._get(
            f"/{settings.META_PAGE_ID}/posts",
            {
                "fields": "id,message,created_time,permalink_url,"
                          "shares,comments.summary(true).limit(0),likes.summary(true).limit(0)",
                "limit": min(limit, 100),
            },
        )
        posts: list[NormalizedPost] = []
        for item in data.get("data", []):
            posts.append(
                NormalizedPost(
                    platform=self.platform,
                    post_id=item["id"],
                    author_id=settings.META_PAGE_ID,
                    author_display_name=page.get("name"),
                    content=item.get("message") or "",
                    posted_at=_parse_ts(item.get("created_time")),
                    url=item.get("permalink_url"),
                    language=None,
                    likes=(item.get("likes") or {}).get("summary", {}).get("total_count"),
                    comments=(item.get("comments") or {}).get("summary", {}).get("total_count"),
                    shares=(item.get("shares") or {}).get("count"),
                    views=None,
                    followers=page.get("fan_count"),
                    unique_sharers=None,
                )
            )
        return posts

    async def fetch_post_metrics(
        self, post_id: str, *, since: datetime | None = None, post_posted_at: datetime | None = None
    ) -> list[NormalizedMetrics]:
        if not self.configured:
            raise ConnectorError("Facebook connector not configured")
        data = await self._get(
            f"/{post_id}",
            {"fields": "shares,comments.summary(true).limit(0),likes.summary(true).limit(0)"},
        )
        page = await self._get(f"/{settings.META_PAGE_ID}", {"fields": "fan_count"})
        return [
            NormalizedMetrics(
                timestamp=datetime.now(timezone.utc),
                likes=(data.get("likes") or {}).get("summary", {}).get("total_count"),
                comments=(data.get("comments") or {}).get("summary", {}).get("total_count"),
                shares=(data.get("shares") or {}).get("count"),
                views=None,
                followers=page.get("fan_count"),
                unique_sharers=None,
            )
        ]

    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        return []  # Facebook does not expose the reshare graph via API

    async def health_check(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(self.platform, configured=False,
                                   detail="META_ACCESS_TOKEN / META_PAGE_ID not set")
        try:
            await self._get(f"/{settings.META_PAGE_ID}", {"fields": "id"})
            return ConnectorStatus(self.platform, configured=True, healthy=True, detail="Graph API reachable")
        except ConnectorError as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False, detail=str(exc))

    async def _get(self, path: str, params: dict) -> dict:
        params = dict(params)
        params["access_token"] = settings.META_ACCESS_TOKEN
        client = await self._http()
        resp = await client.get(f"{GRAPH}{path}", params=params)
        if resp.status_code >= 400:
            raise ConnectorError(f"Facebook Graph {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

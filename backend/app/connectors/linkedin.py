"""LinkedIn connector — official LinkedIn REST API (Community Management).

https://learn.microsoft.com/en-us/linkedin/marketing/
Authentication: OAuth2 access token with w_member_social / w_organization_social.

Available:
    posts by an organization: /rest/posts?author=urn:li:organization:{id}
        fields: id, commentary, createdAt, lifecycleState
    social actions: reactions/comments totals via
        /rest/socialActions/{shareUrn}  (reactionsSummary.totalLikes etc.)
NOT available:
    - reshare counts are NOT returned by current official APIs
      (LinkedIn removed total share counts)               -> shares = None
    - impressions require organizationalEntityShareStatistics which is
      restricted to approved Marketing partners           -> views = None
    - unique sharers                                      -> None
    - propagation graph                                   -> None

This connector degrades gracefully: if a field cannot be fetched it stays None.
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

API_BASE = "https://api.linkedin.com"
LINKEDIN_VERSION = "202405"


class LinkedInConnector(SocialPlatformConnector):
    platform = "linkedin"

    @property
    def configured(self) -> bool:
        return bool(settings.LINKEDIN_ACCESS_TOKEN and settings.LINKEDIN_ORGANIZATION_URN)

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.LINKEDIN_ACCESS_TOKEN}",
            "LinkedIn-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        if not self.configured:
            raise ConnectorError("LinkedIn connector not configured (LINKEDIN_ACCESS_TOKEN / LINKEDIN_ORGANIZATION_URN)")
        author = f"urn:li:organization:{settings.LINKEDIN_ORGANIZATION_URN}"
        data = await self._get(
            "/rest/posts",
            {"author": author, "count": min(limit, 50),
             "fields": "id,commentary,createdAt,lifecycleState"},
        )
        elements = data.get("elements", []) if isinstance(data, dict) else []
        posts: list[NormalizedPost] = []
        for item in elements:
            if item.get("lifecycleState") not in (None, "PUBLISHED"):
                continue
            urn = item.get("id", "")
            likes, comments = await self._social_totals(urn)
            posts.append(
                NormalizedPost(
                    platform=self.platform,
                    post_id=urn,
                    author_id=settings.LINKEDIN_ORGANIZATION_URN,
                    content=item.get("commentary") or "",
                    posted_at=_parse_ms(item.get("createdAt")),
                    url=f"https://www.linkedin.com/feed/update/{urn}",
                    language=None,
                    likes=likes,
                    comments=comments,
                    shares=None,  # not exposed by current official API
                    views=None,
                    followers=await self._follower_count(),
                    unique_sharers=None,
                )
            )
        return posts

    async def fetch_post_metrics(
        self, post_id: str, *, since: datetime | None = None, post_posted_at: datetime | None = None
    ) -> list[NormalizedMetrics]:
        if not self.configured:
            raise ConnectorError("LinkedIn connector not configured")
        likes, comments = await self._social_totals(post_id)
        return [
            NormalizedMetrics(
                timestamp=datetime.now(timezone.utc),
                likes=likes,
                comments=comments,
                shares=None,
                views=None,
                followers=await self._follower_count(),
                unique_sharers=None,
            )
        ]

    async def fetch_propagation_data(
        self, post_id: str, *, since: datetime | None = None
    ) -> list[NormalizedPropagationEvent]:
        return []  # LinkedIn does not expose reshare graphs

    async def health_check(self) -> ConnectorStatus:
        if not self.configured:
            return ConnectorStatus(self.platform, configured=False,
                                   detail="LINKEDIN_ACCESS_TOKEN / LINKEDIN_ORGANIZATION_URN not set")
        try:
            await self._get(f"/rest/organizations/{settings.LINKEDIN_ORGANIZATION_URN}",
                            {"fields": "id,name"})
            return ConnectorStatus(self.platform, configured=True, healthy=True, detail="LinkedIn API reachable")
        except ConnectorError as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False, detail=str(exc))

    # -------------------------------------------------------------- internals
    async def _social_totals(self, post_urn: str) -> tuple[Optional[int], Optional[int]]:
        try:
            data = await self._get(
                f"/rest/socialActions/{post_urn}",
                {"fields": "likesSummary,commentsSummary"},
            )
            likes = (data.get("likesSummary") or {}).get("totalLikes")
            comments = (data.get("commentsSummary") or {}).get("totalFirstLevelComments")
            return likes, comments
        except ConnectorError:
            return None, None

    async def _follower_count(self) -> Optional[int]:
        try:
            data = await self._get(
                f"/rest/organizationalEntityFollowerStatistics",
                {"organizationalEntity": f"urn:li:organization:{settings.LINKEDIN_ORGANIZATION_URN}",
                 "fields": "firstDegreeSize"},
            )
            elements = data.get("elements", []) if isinstance(data, dict) else []
            if elements:
                return elements[0].get("firstDegreeSize")
        except ConnectorError:
            pass
        return None

    async def _get(self, path: str, params: dict) -> dict:
        client = await self._http()
        resp = await client.get(f"{API_BASE}{path}", params=params, headers=self._headers)
        if resp.status_code >= 400:
            raise ConnectorError(f"LinkedIn API {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _parse_ms(value: Optional[int]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)

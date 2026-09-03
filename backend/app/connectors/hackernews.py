"""Hacker News connector — official public Firebase API (keyless, real).

https://github.com/HackerNews/API — free, no auth, no rate-limit key.
Stories expose REAL engagement (score = upvotes, descendants = comments)
that can be re-fetched at any time, giving the pipeline genuine metric
time-series (velocity, acceleration, forecasts) without any credentials.

Mapping honesty: HN has no reshare concept, so `shares` stays None. Score is
mapped to likes and descendants to comments — nothing invented.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    NormalizedMetrics,
    NormalizedPost,
    SocialPlatformConnector,
)
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.connectors.hackernews")

API_BASE = "https://hacker-news.firebaseio.com/v0"
_ITEM_URL = "https://news.ycombinator.com/item?id={id}"


class HackerNewsConnector(SocialPlatformConnector):
    platform = "hackernews"
    supports_discovery = True
    min_poll_seconds = settings.HACKERNEWS_MIN_POLL_SECONDS

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        super().__init__(timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------- fetching
    async def _get(self, path: str) -> Any:
        try:
            client = await self._http()
            resp = await client.get(f"{API_BASE}{path}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Hacker News API error: {exc}",
                                 kind="unavailable") from exc

    async def _item(self, item_id: int | str) -> Optional[dict]:
        item = await self._get(f"/item/{int(item_id)}.json")
        return item if isinstance(item, dict) else None

    @staticmethod
    def _to_post(item: dict) -> NormalizedPost:
        posted = datetime.fromtimestamp(item.get("time", time.time()), tz=timezone.utc)
        return NormalizedPost(
            platform="hackernews",
            post_id=str(item["id"]),
            author_id=str(item.get("by") or "unknown"),
            content=str(item.get("title") or "").strip(),
            posted_at=posted,
            url=item.get("url") or _ITEM_URL.format(id=item["id"]),
            language=None,
            author_display_name=item.get("by"),
            is_demo=False,
            likes=item.get("score"),          # real upvote count
            comments=item.get("descendants"),  # real comment count
            shares=None,                       # HN exposes no reshare count
            views=None,
            followers=None,
            unique_sharers=None,
        )

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        story_ids = await self._get("/topstories.json")
        if not isinstance(story_ids, list):
            raise ConnectorError("Hacker News topstories returned unexpected payload",
                                 kind="unavailable")
        limit = max(1, min(limit, 40))
        semaphore = asyncio.Semaphore(8)

        async def fetch_one(item_id: int) -> Optional[NormalizedPost]:
            async with semaphore:
                try:
                    item = await self._item(item_id)
                except ConnectorError:
                    return None
            if not item or item.get("type") != "story" or item.get("dead"):
                return None
            return self._to_post(item)

        posts = await asyncio.gather(*(fetch_one(i) for i in story_ids[: limit * 2]))
        out = [p for p in posts if p is not None]
        return out[:limit]

    async def fetch_post_metrics(self, post_id: str, *, since=None,
                                 post_posted_at=None) -> list[NormalizedMetrics]:
        item = await self._item(post_id)
        if not item:
            return []
        likes, comments = item.get("score"), item.get("descendants")
        # Store a snapshot only when something actually changed since `since`
        # (the caller passes the latest stored snapshot semantics via time);
        # simplest honest rule: store the fresh REAL observation each pass —
        # the repository dedupes identical timestamps, and unchanged values
        # add no velocity.
        return [NormalizedMetrics(
            timestamp=datetime.now(timezone.utc),
            likes=likes, comments=comments, shares=None,
            views=None, followers=None, unique_sharers=None,
        )]

    async def fetch_propagation_data(self, post_id: str, *, since=None) -> list:
        # HN comment trees are replies, not reshare edges — stay empty.
        return []

    async def health_check(self) -> ConnectorStatus:
        start = time.monotonic()
        try:
            ids = await self._get("/topstories.json")
            ok = isinstance(ids, list) and len(ids) > 0
        except Exception as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False,
                                   detail=f"API unreachable: {exc}")
        detail = "official public Firebase API — real scores/comments, no key required"
        return ConnectorStatus(self.platform, configured=True, healthy=ok,
                               detail=detail if ok else "API returned empty story list",
                               latency_ms=round((time.monotonic() - start) * 1000))

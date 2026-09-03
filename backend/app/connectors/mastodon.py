"""Mastodon connector — public timelines on open instances (keyless, real).

Several public instances expose /api/v1/timelines/public without OAuth.
Statuses carry REAL engagement: reblogs_count (actual shares),
favourites_count (likes), replies_count (comments) — all re-fetchable per
status, producing genuine metric time-series.

External post ids are namespaced `instance:id` so re-fetches always hit the
origin instance even when the same status id exists elsewhere.
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

logger = get_logger("prvision.connectors.mastodon")


class MastodonConnector(SocialPlatformConnector):
    platform = "mastodon"
    supports_discovery = True
    min_poll_seconds = settings.MASTODON_MIN_POLL_SECONDS

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        super().__init__(timeout_seconds=timeout_seconds)

    @property
    def instances(self) -> list[str]:
        return [i.strip() for i in settings.MASTODON_INSTANCES.split(",") if i.strip()]

    async def _get(self, instance: str, path: str, params: dict | None = None) -> Any:
        try:
            client = await self._http()
            resp = await client.get(
                f"https://{instance}{path}", params=params,
                headers={"User-Agent": "PRVisionResearch/1.0 (misinformation early-warning demo)"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Mastodon {instance} error: {exc}",
                                 kind="unavailable") from exc

    @staticmethod
    def _to_post(status: dict, instance: str) -> Optional[NormalizedPost]:
        if status.get("reblog"):
            # Boost wrapper — the original already enters via its own instance.
            return None
        visibility = status.get("visibility", "public")
        if visibility not in ("public", "unlisted"):
            return None  # never ingest private/followers-only content
        content_text = _strip_html(status.get("content") or "")
        if len(content_text) < 25:
            return None
        created = _parse_mastodon_ts(status.get("created_at"))
        account = status.get("account") or {}
        return NormalizedPost(
            platform="mastodon",
            post_id=f"{instance}:{status['id']}",
            author_id=str(account.get("acct") or "unknown"),
            content=content_text,
            posted_at=created,
            url=status.get("url") or f"https://{instance}/@{account.get('acct')}/{status['id']}",
            language=status.get("language"),
            author_display_name=account.get("display_name") or account.get("acct"),
            is_demo=False,
            likes=status.get("favourites_count"),
            comments=status.get("replies_count"),
            shares=status.get("reblogs_count"),  # real boost/share count
            views=None,
            followers=(account.get("followers_count")),
            unique_sharers=None,
        )

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        instances = self.instances
        if not instances:
            raise ConnectorError("No Mastodon instances configured "
                                 "(MASTODON_INSTANCES empty)", kind="unavailable")
        per_instance = max(4, min(20, limit))
        semaphore = asyncio.Semaphore(len(instances))

        async def fetch_one(instance: str) -> list[NormalizedPost]:
            async with semaphore:
                try:
                    statuses = await self._get(
                        instance, "/api/v1/timelines/public",
                        params={"limit": per_instance, "local": "true"})
                except ConnectorError as exc:
                    logger.warning("mastodon timeline %s: %s", instance, exc)
                    return []
            out = []
            for status in statuses or []:
                post = self._to_post(status, instance)
                if post:
                    out.append(post)
            return out

        results = await asyncio.gather(*(fetch_one(i) for i in instances[:3]))
        flat = [p for chunk in results for p in chunk]
        # dedupe across instances
        seen: set[str] = set()
        out = []
        for p in flat:
            if p.post_id in seen:
                continue
            seen.add(p.post_id)
            out.append(p)
        return out[:limit]

    async def fetch_post_metrics(self, post_id: str, *, since=None,
                                 post_posted_at=None) -> list[NormalizedMetrics]:
        if ":" not in post_id:
            return []
        instance, status_id = post_id.split(":", 1)
        try:
            status = await self._get(instance, f"/api/v1/statuses/{status_id}")
        except ConnectorError as exc:
            logger.warning("mastodon metrics %s: %s", post_id, exc)
            return []
        if not isinstance(status, dict):
            return []
        return [NormalizedMetrics(
            timestamp=datetime.now(timezone.utc),
            likes=status.get("favourites_count"),
            comments=status.get("replies_count"),
            shares=status.get("reblogs_count"),
            views=None, followers=None, unique_sharers=None,
        )]

    async def fetch_propagation_data(self, post_id: str, *, since=None) -> list:
        # Reblog edges exist, but the public API does not enumerate who
        # boosted a status without auth (reblogged_by is limited/undocumented
        # on open instances) — stay empty rather than approximate.
        return []

    async def health_check(self) -> ConnectorStatus:
        start = time.monotonic()
        instances = self.instances
        if not instances:
            return ConnectorStatus(self.platform, configured=False,
                                   detail="MASTODON_INSTANCES not set")
        try:
            info = await self._get(instances[0], "/api/v1/instance")
            ok = isinstance(info, dict) and bool(info.get("uri"))
        except Exception as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False,
                                   detail=f"{instances[0]} unreachable: {exc}")
        detail = (f"public timelines on {len(instances)} instance(s) — real "
                  f"reblogs/favourites, no key required")
        return ConnectorStatus(self.platform, configured=True, healthy=ok,
                               detail=detail if ok else "instance info unavailable",
                               latency_ms=round((time.monotonic() - start) * 1000))


_TAG_RE = __import__("re").compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    import html as _html
    text = _TAG_RE.sub(" ", html)
    return " ".join(_html.unescape(text).split())


def _parse_mastodon_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)

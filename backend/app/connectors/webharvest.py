"""Web-search harvest connectors — REAL public posts for the big-5 platforms.

For X, Reddit, Instagram, Facebook and LinkedIn the official APIs all require
paid/OAuth credentials. When those credentials are absent this connector
ingests REAL, publicly indexed posts from each platform through the provider
sidecar's web search (z-ai SDK, keyless, already used by the evidence layer).

Honesty contract (same rules as every other connector):
- URL, author handle, post text and publish time come from the REAL search
  result — never synthesized.
- Engagement counters are stored ONLY when the search result itself exposes
  them ("1.2K likes", "123 votes, 45 comments"); otherwise they stay None.
  A metric the platform does not expose is None — never 0, never invented.
- The post URL always points at the original platform post, so every queue
  row is verifiable with one click.
- When official credentials ARE configured the registry swaps this connector
  for the official API adapter (see connectors/__init__._build).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.connectors.base import (
    HARVEST_PLATFORMS,
    ConnectorError,
    ConnectorStatus,
    NormalizedPost,
    SocialPlatformConnector,
)
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.connectors.webharvest")


# --------------------------------------------------------------------- topics
# Rotating query topics keep successive harvest cycles varied so the corpus
# grows across the misinformation-relevant space instead of re-reading the
# same handful of results.
TOPICS = (
    "breaking news",
    "viral claim",
    "election",
    "health advice",
    "vaccine",
    "climate",
    "AI technology",
    "crypto",
    "sports result",
    "celebrity",
    "finance markets",
    "world conflict",
    "weather warning",
    "science study",
    "food safety",
    "protest",
)


# ---------------------------------------------------------------- URL matchers
@dataclass(frozen=True)
class HarvestMatch:
    external_id: str
    author_id: str
    author_display: Optional[str] = None


def _match_x(url_path: str, url: str) -> Optional[HarvestMatch]:
    m = re.match(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)", url_path)
    if not m:
        return None
    return HarvestMatch(external_id=m.group(2), author_id="@" + m.group(1))


def _match_reddit(url_path: str, url: str) -> Optional[HarvestMatch]:
    m = re.match(r"^/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)", url_path)
    if not m:
        return None
    return HarvestMatch(external_id=m.group(2), author_id="r/" + m.group(1))


def _match_instagram(url_path: str, url: str) -> Optional[HarvestMatch]:
    m = re.match(r"^/(?:[A-Za-z0-9_.]+/)?(p|reel|tv)/([A-Za-z0-9_-]+)", url_path)
    if not m:
        return None
    return HarvestMatch(external_id=m.group(2), author_id="instagram")


def _match_facebook(url_path: str, url: str) -> Optional[HarvestMatch]:
    # /{page}/posts/{id} — modern ids may be numeric OR pfbid/hex codes and
    # may carry a trailing numeric segment (/posts/{code}/{id}).
    m = re.match(r"^/([^/]+)/posts/([A-Za-z0-9_-]+)(?:/\d+)?", url_path)
    if m:
        return HarvestMatch(external_id=m.group(2), author_id=m.group(1))
    # /share/p/{id} | /share/v/{id}
    m = re.match(r"^/share/(?:p|v)/([A-Za-z0-9_-]+)", url_path)
    if m:
        return HarvestMatch(external_id=m.group(1), author_id="facebook")
    # /reel/{id} | /watch/?v={id}
    m = re.match(r"^/reel/(\d+)", url_path)
    if m:
        return HarvestMatch(external_id=m.group(1), author_id="facebook")
    m = re.match(r"^/watch/?", url_path)
    if m:
        m2 = re.search(r"[?&]v=(\d+)", url)
        if m2:
            return HarvestMatch(external_id=m2.group(1), author_id="facebook")
    # /permalink.php?story_fbid=… | /story.php?id=… (query lives in `url`)
    if url_path.startswith("/permalink.php") or url_path.startswith("/story.php"):
        m2 = re.search(r"[?&](?:story_)?fbid=(\d+)|[?&]id=(\d+)", url)
        if m2:
            return HarvestMatch(external_id=m2.group(1) or m2.group(2), author_id="facebook")
    # /{page}/photos/{slug}/{id}
    m = re.match(r"^/([^/]+)/photos/[^/]+/(\d+)", url_path)
    if m:
        return HarvestMatch(external_id=m.group(2), author_id=m.group(1))
    return None


def _match_linkedin(url_path: str, url: str) -> Optional[HarvestMatch]:
    m = re.match(r"^/posts/([A-Za-z0-9_%.-]{3,80})", url_path)
    if not m:
        m = re.match(r"^/feed/update/urn:li:(?:ugcPost|share):(\d+)", url_path)
        if not m:
            return None
        return HarvestMatch(external_id=m.group(1), author_id="linkedin")
    slug = m.group(1)
    author = slug.split("-")[0] if "-" in slug else "linkedin"
    return HarvestMatch(external_id=slug[:180], author_id=author)


_MATCHERS = {
    "x": _match_x,
    "reddit": _match_reddit,
    "instagram": _match_instagram,
    "facebook": _match_facebook,
    "linkedin": _match_linkedin,
}

# Canonical hosts per platform (search engines may return regional mirrors).
_ALLOWED_HOSTS = {
    "x": {"x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"},
    "reddit": {"reddit.com", "www.reddit.com", "old.reddit.com"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"},
    "linkedin": {"linkedin.com", "www.linkedin.com"},
}


def match_post_url(platform: str, url: str) -> Optional[HarvestMatch]:
    """Return the harvest match for a canonical platform post URL, else None.

    Profile pages, homepages and non-post URLs are rejected — only links that
    identify a SINGLE post enter the monitoring queue.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
        path = urlparse(url).path or ""
    except ValueError:
        return None
    if host not in _ALLOWED_HOSTS.get(platform, set()):
        return None
    if not path or path == "/":
        return None
    return _MATCHERS[platform](path, url)


# ------------------------------------------------------------ result parsing
_RELATIVE_PATTERNS = (
    (re.compile(r"\b(\d{1,3})\s*min(?:ute)?s?\s+ago\b", re.I), 60),
    (re.compile(r"\b(\d{1,2})\s*hours?\s+ago\b", re.I), 3600),
    (re.compile(r"\b(\d{1,3})\s*hours?\b", re.I), 3600),
    (re.compile(r"\b(\d{1,2})\s*days?\s+ago\b", re.I), 86400),
    (re.compile(r"\b(\d{1,2})\s*days?\b", re.I), 86400),
    (re.compile(r"\b(\d{1,2})\s*w(?:eeks?)?\s+ago\b", re.I), 604800),
)
_SHORT_AGO = re.compile(r"\b(\d{1,3})([mhd])\b\s*(?:ago)?", re.I)
_SHORT_AGO_UNITS = {"m": 60, "h": 3600, "d": 86400}
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_NUM = re.compile(r"([\d][\d.,]*)\s*([KMB])?\s*(likes?|reactions?|upvotes?|votes?|points?|comments?|replies?|reposts?|shares?|boosts?)", re.I)
_KIND_MAP = {
    "like": "likes", "likes": "likes", "reaction": "likes", "reactions": "likes",
    "upvote": "likes", "upvotes": "likes", "vote": "likes", "votes": "likes",
    "point": "likes", "points": "likes",
    "comment": "comments", "comments": "comments", "reply": "comments", "replies": "comments",
    "repost": "shares", "reposts": "shares", "share": "shares", "shares": "shares",
    "boost": "shares", "boosts": "shares",
}


def _parse_count_token(value: str, suffix: str) -> int:
    num = float(value.replace(",", ""))
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix.upper(), 1)
    return int(num * mult)


def parse_engagement(text: str) -> dict[str, int]:
    """Extract engagement counts explicitly present in a search snippet.

    Only counts the snippet literally states are returned — anything absent
    stays absent (the caller leaves those metrics None).
    """
    found: dict[str, int] = {}
    for m in _NUM.finditer(text or ""):
        kind = _KIND_MAP.get(m.group(3).lower())
        if not kind:
            continue
        try:
            value = _parse_count_token(m.group(1), m.group(2) or "")
        except ValueError:
            continue
        # First (usually largest/authoritative) mention wins; Reddit lists
        # votes before comments, X lists likes before comments, etc.
        found.setdefault(kind, value)
    return found


def parse_published_at(*, date_field: str = "", text: str = "",
                       fallback: Optional[datetime] = None) -> datetime:
    """Best-effort REAL publish time: ISO date, then relative phrases.

    Falls back to `fallback` (harvest time = first-seen) when the result
    carries no usable time information.
    """
    now = fallback or datetime.now(timezone.utc)
    if date_field:
        iso = _ISO_DATE.search(date_field)
        if iso:
            try:
                y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
                hh = int(iso.group(4) or 0)
                mm = int(iso.group(5) or 0)
                return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc)
            except ValueError:
                pass
        iso = _ISO_DATE.search(text or "")
        if iso:
            try:
                return datetime(int(iso.group(1)), int(iso.group(2)),
                                int(iso.group(3)), tzinfo=timezone.utc)
            except ValueError:
                pass
        for pattern, unit in _RELATIVE_PATTERNS:
            m = pattern.search(date_field)
            if m:
                return now - timedelta(seconds=int(m.group(1)) * unit)
    for pattern, unit in _RELATIVE_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return now - timedelta(seconds=int(m.group(1)) * unit)
    m = _SHORT_AGO.search(text or "")
    if m:
        seconds = int(m.group(1)) * _SHORT_AGO_UNITS.get(m.group(2).lower(), 0)
        if seconds:
            return now - timedelta(seconds=seconds)
    return now


_DISPLAY_SUFFIX = re.compile(
    r"\s*(?:[/|—-]|·)\s*(?:on\s+)?(X|Twitter|Reddit|Instagram|Facebook|LinkedIn)\s*$", re.I)
_AUTHOR_HANDLE = re.compile(r"\s*\(@([A-Za-z0-9_]{1,30})\)\s*")


def clean_title(title: str) -> tuple[str, Optional[str]]:
    """Return (display_title, author_display_name) from a result title."""
    title = (title or "").strip()
    author = None
    handle = _AUTHOR_HANDLE.search(title)
    if handle:
        author = "@" + handle.group(1)
        title = _AUTHOR_HANDLE.sub(" ", title)
    title = _DISPLAY_SUFFIX.sub("", title)
    return title.strip(), author


# ---------------------------------------------------------------- connector
class WebHarvestConnector(SocialPlatformConnector):
    """Harvests REAL public posts for one platform via sidecar web search.

    Static by design: search results carry no metric stream, so
    fetch_post_metrics returns no new snapshots (the ingested values stay the
    truthful single observation). Official-API connectors provide the live
    metric upgrade path.
    """

    supports_discovery = True
    min_poll_seconds = 0.0  # metrics pass is a no-op — keep it free

    def __init__(self, platform: str) -> None:
        if platform not in HARVEST_PLATFORMS:
            raise ValueError(f"WebHarvestConnector cannot harvest platform: {platform}")
        super().__init__(timeout_seconds=settings.SIDECAR_TIMEOUT_SECONDS)
        self.platform = platform
        self._topic_index = abs(hash(platform)) % len(TOPICS)
        self._last_counts: dict[str, int] = {}
        self.last_harvest_at: Optional[datetime] = None
        self.last_harvest_new: int = 0

    # ------------------------------------------------------------ searching
    def _queries(self, count: int) -> list[str]:
        platform_queries = {
            # twitter.com status:… queries surface real single-post URLs far
            # more often than x.com ones (x.com results skew to profile pages).
            "x": ['site:twitter.com status "{topic}"', 'site:x.com "{topic}"'],
            "reddit": ['site:reddit.com comments "{topic}"', 'site:reddit.com/r/ "{topic}"'],
            "instagram": ['site:instagram.com "/p/" "{topic}"', 'site:instagram.com "{topic}"'],
            "facebook": ['facebook.com posts "{topic}"', 'site:facebook.com "{topic}"'],
            "linkedin": ['site:linkedin.com/posts "{topic}"'],
        }
        templates = platform_queries[self.platform]
        out = []
        for i in range(count):
            topic = TOPICS[(self._topic_index + i) % len(TOPICS)]
            out.append(templates[i % len(templates)].format(topic=topic))
        return out

    async def _search(self, query: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{settings.SIDECAR_URL.rstrip('/')}/web_search",
                    json={"query": query, "num": settings.HARVEST_SEARCH_NUM,
                          "recency_days": settings.HARVEST_RECENCY_DAYS},
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise ConnectorError(
                f"webharvest[{self.platform}]: sidecar search failed: {exc}",
                kind="unavailable") from exc
        if not payload.get("ok"):
            raise ConnectorError(
                f"webharvest[{self.platform}]: sidecar error: {payload.get('error')}",
                kind="unavailable")
        return payload.get("data") or []

    async def fetch_posts(self, *, limit: int = 20, **kwargs: Any) -> list[NormalizedPost]:
        """One discovery pass: rotate topics, keep only real single-post URLs."""
        count = max(1, min(4, -(-limit // max(1, settings.HARVEST_SEARCH_NUM))))
        queries = self._queries(count)
        self._topic_index = (self._topic_index + count) % len(TOPICS)

        posts: list[NormalizedPost] = []
        seen_ids: set[str] = set()
        for query in queries:
            try:
                results = await self._search(query)
            except ConnectorError as exc:
                logger.warning("%s", exc)
                continue
            now = datetime.now(timezone.utc)
            for result in results:
                url = (result.get("url") or "").strip()
                match = match_post_url(self.platform, url)
                if not match or match.external_id in seen_ids:
                    continue
                title, title_author = clean_title(result.get("name") or "")
                snippet = (result.get("snippet") or "").strip()
                if len(title) + len(snippet) < 25:
                    continue  # near-empty index card — not a usable post
                engagement = parse_engagement(" ".join([title, snippet]))
                seen_ids.add(match.external_id)
                posts.append(NormalizedPost(
                    platform=self.platform,
                    post_id=match.external_id,
                    author_id=match.author_id,
                    author_display_name=title_author or match.author_display,
                    content=" — ".join(part for part in (title, snippet) if part),
                    posted_at=parse_published_at(
                        date_field=result.get("date") or "", text=f"{title} {snippet}",
                        fallback=now),
                    url=url,
                    language=None,  # unknown until content analysis runs
                    is_demo=False,
                    likes=engagement.get("likes"),
                    comments=engagement.get("comments"),
                    shares=engagement.get("shares"),
                ))
                if len(posts) >= limit:
                    return posts
        self.last_harvest_at = datetime.now(timezone.utc)
        self.last_harvest_new = len(posts)
        return posts

    # -------------------------------------------------------------- metrics
    async def fetch_post_metrics(self, post_id: str, *, since=None,
                                 post_posted_at=None) -> list:
        """No metric stream without official API — never fabricate deltas."""
        return []

    async def fetch_propagation_data(self, post_id: str, *, since=None) -> list:
        return []

    async def health_check(self) -> ConnectorStatus:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{settings.SIDECAR_URL.rstrip('/')}/health")
                resp.raise_for_status()
                ok = bool(resp.json().get("ok"))
        except Exception as exc:
            return ConnectorStatus(self.platform, configured=True, healthy=False,
                                   detail=f"sidecar unreachable: {exc}")
        if not ok:
            return ConnectorStatus(self.platform, configured=True, healthy=False,
                                   detail="sidecar SDK not ready")
        detail = (f"real posts via web-search harvester "
                  f"({self.last_harvest_new} new at last pass)")
        return ConnectorStatus(self.platform, configured=True, healthy=True,
                               detail=detail,
                               latency_ms=round((time.monotonic() - start) * 1000))

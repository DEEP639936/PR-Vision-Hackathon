"""SearchProvider abstraction (spec #6, #7, #27).

The evidence engine never hard-codes a single search provider. Providers are
pluggable, health-reporting, and configurable through .env:

    SearchProvider
    ├── ZaiWebSearchProvider     (provider sidecar — keyless, sandbox default)
    ├── GoogleFactCheckProvider  (GOOGLE_FACTCHECK_API_KEY)
    ├── WikipediaProvider        (keyless public REST API)
    ├── NewsApiProvider          (NEWSAPI_KEY)
    └── DisabledProvider         (honest placeholder when unconfigured)

Every provider returns EvidenceResult objects carrying a source
classification (LIVE / EXTERNAL_EVIDENCE / SIMULATED) so the frontend can
label provenance honestly (spec #29). No provider is invented at runtime —
if a search cannot be executed the state says so explicitly.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.evidence.providers")

_FETCH_UA = "PRVisionEarlyWarning/1.0 (verification research)"

# ---------------------------------------------------------- probe health cache
# Keyed providers are health-probed with a real minimal API call, cached for
# PROBE_TTL_SECONDS so repeated /health calls never burn provider quotas.
PROBE_TTL_SECONDS = 300.0
_probe_cache: dict[str, tuple[float, "ProviderStatus"]] = {}


def _reset_probe_cache() -> None:
    """Clear cached probe results (used by tests and after key rotation)."""
    _probe_cache.clear()


async def _cached_probe(name: str, probe: Callable[[], Awaitable["ProviderStatus"]]) -> "ProviderStatus":
    now = time.monotonic()
    hit = _probe_cache.get(name)
    if hit and now - hit[0] < PROBE_TTL_SECONDS:
        return hit[1]
    status = await probe()
    _probe_cache[name] = (now, status)
    return status


@dataclass
class EvidenceResult:
    provider: str
    url: Optional[str]
    title: Optional[str]
    snippet: Optional[str]
    publisher: Optional[str]
    published_at: Optional[str]          # raw provider date string
    relevance: float = 0.0               # 0-1 provider-side match strength
    source_classification: str = "EXTERNAL_EVIDENCE"
    note: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ProviderStatus:
    name: str
    state: str                            # CONNECTED|DEGRADED|RATE_LIMITED|AUTH_REQUIRED|UNAVAILABLE|DISABLED
    detail: Optional[str] = None


class SearchProvider(ABC):
    """Common interface for all evidence/search providers (spec #26 analogue)."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    async def search(self, query: str, num: int = 6, recency_days: Optional[int] = None) -> list[EvidenceResult]: ...

    async def health(self) -> ProviderStatus:
        if not self.is_configured():
            return ProviderStatus(self.name, "DISABLED", "no credentials / bridge unavailable")
        return ProviderStatus(self.name, "CONNECTED")


# ------------------------------------------------------------------ sidecar helpers
async def _sidecar_call(endpoint: str, payload: dict, timeout: Optional[float] = None) -> Optional[dict]:
    """Call the local provider sidecar; None on unavailability (caller degrades)."""
    if not settings.SIDECAR_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout or settings.SIDECAR_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{settings.SIDECAR_URL.rstrip('/')}/{endpoint}", json=payload)
            data = resp.json()
        return data if data.get("ok") else None
    except Exception:
        return None


async def sidecar_healthy() -> bool:
    if not settings.SIDECAR_ENABLED:
        return False
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{settings.SIDECAR_URL.rstrip('/')}/health")
            return bool(resp.json().get("data", {}).get("sdk_ready"))
    except Exception:
        return False


class ZaiWebSearchProvider(SearchProvider):
    """General web evidence via the provider sidecar (keyless)."""

    name = "zai_web_search"

    def is_configured(self) -> bool:
        return settings.SIDECAR_ENABLED

    async def search(self, query: str, num: int = 6, recency_days: Optional[int] = None) -> list[EvidenceResult]:
        data = await _sidecar_call("web_search", {"query": query, "num": num, "recency_days": recency_days})
        if not data:
            return []
        out: list[EvidenceResult] = []
        for r in (data.get("data") or [])[:num]:
            out.append(EvidenceResult(
                provider=self.name,
                url=r.get("url"),
                title=r.get("name"),
                snippet=r.get("snippet"),
                publisher=r.get("host"),
                published_at=r.get("date") or None,
                relevance=round(max(0.1, 1.0 - 0.1 * (r.get("rank") or 0)), 2),
            ))
        return out

    async def health(self) -> ProviderStatus:
        if await sidecar_healthy():
            return ProviderStatus(self.name, "CONNECTED")
        return ProviderStatus(self.name, "UNAVAILABLE", "sidecar bridge unreachable")


class ZaiPageReader:
    """Article fetch through the sidecar page reader (keyless, JS-tolerant)."""

    name = "zai_page_reader"

    async def read(self, url: str) -> Optional[dict]:
        data = await _sidecar_call("page_reader", {"url": url})
        if not data:
            return None
        d = data.get("data") or {}
        return {"url": d.get("url") or url, "title": d.get("title"), "html": d.get("html") or "",
                "published_time": d.get("published_time"), "provider": self.name}


class WikipediaProvider(SearchProvider):
    """Keyless public REST search — reliable for entities/events/background."""

    name = "wikipedia"

    def is_configured(self) -> bool:
        return True

    async def search(self, query: str, num: int = 4, recency_days: Optional[int] = None) -> list[EvidenceResult]:
        try:
            async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": _FETCH_UA}) as client:
                resp = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": query[:220], "limit": min(num, 8)},
                )
                if resp.status_code == 429:
                    return []
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            return []
        out: list[EvidenceResult] = []
        for page in (body.get("pages") or [])[:num]:
            out.append(EvidenceResult(
                provider=self.name,
                url=f"https://en.wikipedia.org/wiki/{(page.get('key') or '').replace(' ', '_')}",
                title=page.get("title"),
                snippet=(page.get("description") or page.get("excerpt") or "")[:400],
                publisher="Wikipedia",
                published_at=None,
                relevance=0.55,
            ))
        return out


class GoogleFactCheckProvider(SearchProvider):
    """Google Fact Check Tools API (spec #7) — requires GOOGLE_FACTCHECK_API_KEY."""

    name = "google_factcheck"

    def is_configured(self) -> bool:
        return bool(settings.GOOGLE_FACTCHECK_API_KEY)

    async def search(self, query: str, num: int = 5, recency_days: Optional[int] = None) -> list[EvidenceResult]:
        return await self.fact_checks(query, num)

    async def fact_checks(self, claim: str, num: int = 5) -> list[dict]:
        """Return raw fact-check review dicts (not generic evidence)."""
        if not self.is_configured():
            return []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://factchecktools.googleapis.com/v1alpha1/claims:search",
                    params={"query": claim[:400], "key": settings.GOOGLE_FACTCHECK_API_KEY, "pageSize": num},
                )
                if resp.status_code == 429:
                    return []
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:
            logger.info("google_factcheck failed: %s", exc.__class__.__name__)
            return []
        out: list[dict] = []
        for c in body.get("claims", [])[:num]:
            review = (c.get("claimReview") or [{}])[0]
            out.append({
                "claim_text": c.get("text"),
                "textual_rating": (review.get("textualRating") or "")[:120],
                "publisher": ((review.get("publisher") or {}).get("name")) or None,
                "published_at": review.get("publishDate"),
                "url": review.get("url"),
                "snippet": (review.get("title") or "")[:400],
                "provider": self.name,
            })
        return out

    async def _probe(self) -> ProviderStatus:
        """Real minimal API call so CONNECTED means the key was accepted."""
        try:
            async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _FETCH_UA}) as client:
                resp = await client.get(
                    "https://factchecktools.googleapis.com/v1alpha1/claims:search",
                    params={"query": "climate", "pageSize": 1, "key": settings.GOOGLE_FACTCHECK_API_KEY},
                )
        except Exception as exc:
            return ProviderStatus(self.name, "UNAVAILABLE", f"probe failed: {exc.__class__.__name__}")
        if resp.status_code == 200:
            return ProviderStatus(self.name, "CONNECTED", "live probe ok")
        if resp.status_code in (400, 401, 403):
            # Google signals an invalid key with HTTP 400 + "API key not valid".
            try:
                msg = ((resp.json().get("error") or {}).get("message") or "")[:120]
            except Exception:
                msg = ""
            return ProviderStatus(self.name, "AUTH_REQUIRED", msg or f"key rejected (HTTP {resp.status_code})")
        if resp.status_code == 429:
            return ProviderStatus(self.name, "RATE_LIMITED", "quota exceeded — retry later")
        return ProviderStatus(self.name, "UNAVAILABLE", f"probe HTTP {resp.status_code}")

    async def health(self) -> ProviderStatus:
        if not self.is_configured():
            return ProviderStatus(self.name, "DISABLED",
                                  "no key — set GOOGLE_FACTCHECK_API_KEY in .env (console.cloud.google.com)")
        return await _cached_probe(self.name, self._probe)


class NewsApiProvider(SearchProvider):
    """News search (NEWSAPI_KEY) — recency-weighted news evidence."""

    name = "newsapi"

    def is_configured(self) -> bool:
        return bool(settings.NEWSAPI_KEY)

    async def search(self, query: str, num: int = 6, recency_days: Optional[int] = None) -> list[EvidenceResult]:
        if not self.is_configured():
            return []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": query[:380], "pageSize": min(num, 20),
                        "sortBy": "relevancy",
                        "apiKey": settings.NEWSAPI_KEY,
                    },
                )
                if resp.status_code == 429:
                    return []
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            return []
        out: list[EvidenceResult] = []
        for a in (body.get("articles") or [])[:num]:
            out.append(EvidenceResult(
                provider=self.name,
                url=a.get("url"),
                title=a.get("title"),
                snippet=a.get("description"),
                publisher=(a.get("source") or {}).get("name"),
                published_at=a.get("publishedAt"),
                relevance=0.6,
            ))
        return out

    async def _probe(self) -> ProviderStatus:
        """Real minimal API call so CONNECTED means the key was accepted."""
        try:
            async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _FETCH_UA}) as client:
                resp = await client.get(
                    "https://newsapi.org/v2/top-headlines",
                    params={"pageSize": 1, "apiKey": settings.NEWSAPI_KEY},
                )
        except Exception as exc:
            return ProviderStatus(self.name, "UNAVAILABLE", f"probe failed: {exc.__class__.__name__}")
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code == 200:
            if body.get("status") == "ok":
                return ProviderStatus(self.name, "CONNECTED", "live probe ok")
            err = body.get("error") or {}
            code = err.get("code") or body.get("code") or "error"
            msg = err.get("message") or body.get("message") or ""
            return ProviderStatus(self.name, "AUTH_REQUIRED", f"{code}: {msg[:100]}")
        if resp.status_code in (401, 403):
            err = body.get("error") or {}
            code = (err.get("code") or body.get("code")
                    or f"key rejected (HTTP {resp.status_code})")
            return ProviderStatus(self.name, "AUTH_REQUIRED", code)
        if resp.status_code == 429:
            return ProviderStatus(self.name, "RATE_LIMITED", "quota exceeded — retry later")
        return ProviderStatus(self.name, "UNAVAILABLE", f"probe HTTP {resp.status_code}")

    async def health(self) -> ProviderStatus:
        if not self.is_configured():
            return ProviderStatus(self.name, "DISABLED",
                                  "no key — set NEWSAPI_KEY in .env (newsapi.org/register)")
        return await _cached_probe(self.name, self._probe)


class EvidenceRetriever:
    """Fan-out orchestrator with per-claim query construction (spec #6)."""

    def __init__(self) -> None:
        self.providers: list[SearchProvider] = [
            ZaiWebSearchProvider(),
            WikipediaProvider(),
            NewsApiProvider(),
        ]
        self.factcheck = GoogleFactCheckProvider()

    async def health(self) -> list[ProviderStatus]:
        """Health for every provider class, in stable display order:
        zai_web_search, wikipedia, google_factcheck, newsapi."""
        zai, wiki, news = self.providers
        return list(await asyncio.gather(
            zai.health(), wiki.health(), self.factcheck.health(), news.health(),
        ))

    @staticmethod
    def build_query(claim_text: str, entities: list[dict[str, str]], max_terms: int = 18) -> str:
        """Compact, high-signal search query from a claim."""
        ent_names = [e.get("name", "") for e in (entities or [])][:4]
        terms = " ".join(ent_names + [claim_text])
        words = [w for w in terms.split() if w][:max_terms]
        return " ".join(words)

    async def retrieve_for_claim(
        self, claim_text: str, entities: list[dict[str, str]], num_per_provider: int = 4,
    ) -> tuple[list[EvidenceResult], list[dict]]:
        """Evidence + fact-checks for one claim. Degrades honestly per provider."""
        query = self.build_query(claim_text, entities)
        tasks = [p.search(query, num=num_per_provider) for p in self.providers if p.is_configured()]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)
        evidence: list[EvidenceResult] = []
        for item in results_nested:
            if isinstance(item, Exception):
                continue
            evidence.extend(item)
        fact_checks: list[dict] = []
        if self.factcheck.is_configured() and claim_text:
            fact_checks = await self.factcheck.fact_checks(claim_text, num=4)
        return evidence, fact_checks

    async def retrieve_general(self, topic: str, num: int = 6) -> list[EvidenceResult]:
        """Job-level evidence (context around the whole content)."""
        tasks = [p.search(topic, num=num) for p in self.providers if p.is_configured()]
        results_nested = await asyncio.gather(*tasks, return_exceptions=True)
        evidence: list[EvidenceResult] = []
        for item in results_nested:
            if isinstance(item, Exception):
                continue
            evidence.extend(item)
        return evidence

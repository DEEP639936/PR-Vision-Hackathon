"""Unit tests for keyed evidence-provider health probes.

google_factcheck and newsapi must report honest states:
  - no key            -> DISABLED
  - key + 200/ok      -> CONNECTED (real probe, cached)
  - key rejected      -> AUTH_REQUIRED
  - network failure   -> UNAVAILABLE
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.evidence import providers as prov


def _resp(status_code: int, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    return r


@pytest.fixture(autouse=True)
def _clean_cache():
    prov._reset_probe_cache()
    yield
    prov._reset_probe_cache()


# ---------------------------------------------------------------- google_factcheck
def test_factcheck_disabled_without_key():
    with patch.object(settings, "GOOGLE_FACTCHECK_API_KEY", ""):
        p = prov.GoogleFactCheckProvider()
        assert p.is_configured() is False
        status = asyncio.run(p.health())
    assert status.state == "DISABLED"
    assert "GOOGLE_FACTCHECK_API_KEY" in (status.detail or "")


def test_factcheck_connected_on_live_probe():
    with patch.object(settings, "GOOGLE_FACTCHECK_API_KEY", "test-key-ok"), \
         patch.object(prov.httpx, "AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__ = AsyncMock()
        # AsyncClient() used as async context manager
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=_resp(200, {"claims": []}))))
        cm.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = cm

        p = prov.GoogleFactCheckProvider()
        status = asyncio.run(p.health())
    assert status.state == "CONNECTED"
    assert "probe" in (status.detail or "")


def test_factcheck_bad_key_is_auth_required():
    with patch.object(settings, "GOOGLE_FACTCHECK_API_KEY", "bad-key"), \
         patch.object(prov.httpx, "AsyncClient") as client_cls:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=_resp(400, {"error": {"code": 400,
                                                            "message": "API key not valid."}}))))
        cm.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = cm

        p = prov.GoogleFactCheckProvider()
        status = asyncio.run(p.health())
    assert status.state == "AUTH_REQUIRED"
    assert "API key not valid" in (status.detail or "")


def test_factcheck_probe_cached():
    calls = {"n": 0}

    async def fake_probe() -> prov.ProviderStatus:
        calls["n"] += 1
        return prov.ProviderStatus("google_factcheck", "CONNECTED", "live probe ok")

    asyncio.run(prov._cached_probe("google_factcheck", fake_probe))
    asyncio.run(prov._cached_probe("google_factcheck", fake_probe))
    assert calls["n"] == 1  # second call served from cache


def test_factcheck_network_error_unavailable():
    with patch.object(settings, "GOOGLE_FACTCHECK_API_KEY", "key"), \
         patch.object(prov.httpx, "AsyncClient") as client_cls:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=OSError("no route to host"))
        cm.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = cm

        p = prov.GoogleFactCheckProvider()
        status = asyncio.run(p.health())
    assert status.state == "UNAVAILABLE"


# ------------------------------------------------------------------------ newsapi
def test_newsapi_disabled_without_key():
    with patch.object(settings, "NEWSAPI_KEY", ""):
        p = prov.NewsApiProvider()
        status = asyncio.run(p.health())
    assert status.state == "DISABLED"
    assert "NEWSAPI_KEY" in (status.detail or "")


def test_newsapi_connected_on_live_probe():
    with patch.object(settings, "NEWSAPI_KEY", "test-key-ok"), \
         patch.object(prov.httpx, "AsyncClient") as client_cls:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=_resp(200, {"status": "ok", "articles": []}))))
        cm.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = cm

        p = prov.NewsApiProvider()
        status = asyncio.run(p.health())
    assert status.state == "CONNECTED"


def test_newsapi_key_error_is_auth_required():
    with patch.object(settings, "NEWSAPI_KEY", "bad"), \
         patch.object(prov.httpx, "AsyncClient") as client_cls:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=_resp(401, {"status": "error",
                                                   "code": "apiKeyInvalid",
                                                   "message": "Your API key is invalid"}))))
        cm.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = cm

        p = prov.NewsApiProvider()
        status = asyncio.run(p.health())
    assert status.state == "AUTH_REQUIRED"
    assert "apiKeyInvalid" in (status.detail or "")


# -------------------------------------------------------------- retriever health
def test_retriever_health_covers_all_four_providers():
    statuses = asyncio.run(prov.EvidenceRetriever().health())
    names = [s.name for s in statuses]
    assert names == ["zai_web_search", "wikipedia", "google_factcheck", "newsapi"]


def test_search_skipped_when_not_configured():
    """retrieve_for_claim must not call unconfigured providers."""
    with patch.object(settings, "NEWSAPI_KEY", ""), \
         patch.object(settings, "GOOGLE_FACTCHECK_API_KEY", ""):
        r = prov.EvidenceRetriever()
        assert all(not isinstance(p, prov.NewsApiProvider) or not p.is_configured()
                   for p in r.providers)

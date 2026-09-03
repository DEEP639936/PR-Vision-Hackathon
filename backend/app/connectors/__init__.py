"""Connector registry — single place where platform adapters are constructed.

Big-5 platforms (x/reddit/instagram/facebook/linkedin) use their OFFICIAL API
adapter when credentials exist; without credentials they fall back to the
keyless WebHarvestConnector, which ingests REAL public posts via the provider
sidecar's web search. Restart after adding credentials to swap adapters.
"""
from __future__ import annotations

from typing import Optional

from app.connectors.base import (
    HARVEST_PLATFORMS,
    SUPPORTED_PLATFORMS,
    SocialPlatformConnector,
)
from app.connectors.demo import ARCHETYPES, DemoConnector
from app.connectors.facebook import FacebookConnector
from app.connectors.hackernews import HackerNewsConnector
from app.connectors.instagram import InstagramConnector
from app.connectors.linkedin import LinkedInConnector
from app.connectors.mastodon import MastodonConnector
from app.connectors.reddit import RedditConnector
from app.connectors.webharvest import WebHarvestConnector
from app.connectors.x import XConnector

__all__ = [
    "SUPPORTED_PLATFORMS",
    "HARVEST_PLATFORMS",
    "ARCHETYPES",
    "SocialPlatformConnector",
    "DemoConnector",
    "XConnector",
    "RedditConnector",
    "InstagramConnector",
    "FacebookConnector",
    "LinkedInConnector",
    "MastodonConnector",
    "HackerNewsConnector",
    "WebHarvestConnector",
    "get_connector",
]

_instances: dict[str, SocialPlatformConnector] = {}


def get_connector(platform: str) -> SocialPlatformConnector:
    """Return a cached connector instance for a platform."""
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")
    if platform not in _instances:
        _instances[platform] = _build(platform)
    return _instances[platform]


def _build(platform: str) -> SocialPlatformConnector:
    from app.core.config import settings

    builders = {
        "demo": DemoConnector,
        "mastodon": MastodonConnector,
        "hackernews": HackerNewsConnector,
    }
    factory = builders.get(platform)
    if factory:
        return factory()
    if platform in HARVEST_PLATFORMS:
        # Official API adapter when credentials are configured; keyless real-
        # post harvester otherwise (honest status text explains the method).
        if settings.is_platform_configured(platform):
            official = {
                "x": XConnector,
                "reddit": RedditConnector,
                "instagram": InstagramConnector,
                "facebook": FacebookConnector,
                "linkedin": LinkedInConnector,
            }
            return official[platform]()
        return WebHarvestConnector(platform)
    raise ValueError(f"Unsupported platform: {platform}")


async def close_all() -> None:
    for connector in _instances.values():
        await connector.aclose()
    _instances.clear()

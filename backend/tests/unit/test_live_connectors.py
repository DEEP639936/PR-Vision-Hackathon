"""Unit tests for the keyless public-API connectors (mastodon, hackernews).

All HTTP is mocked at the connector's `_get` boundary — no network.
"""
from __future__ import annotations

import asyncio

import pytest

from app.connectors.base import ConnectorError
from app.connectors.hackernews import HackerNewsConnector
from app.connectors.mastodon import MastodonConnector


# --------------------------------------------------------------- hackernews
def test_hn_fetch_posts_maps_real_fields(monkeypatch):
    conn = HackerNewsConnector()

    async def fake_get(path):
        if path == "/topstories.json":
            return [1, 2, 3]
        if path == "/item/1.json":
            return {"id": 1, "type": "story", "title": "Google avoids a breakup",
                    "by": "person1", "time": 1788000000, "score": 295,
                    "descendants": 213, "url": "https://example.com/a"}
        if path == "/item/2.json":
            return {"id": 2, "type": "story", "title": "Ask HN: anything",
                    "by": "person2", "time": 1788000100, "score": 10,
                    "descendants": 4}
        if path == "/item/3.json":
            return {"id": 3, "type": "job", "title": "Hiring"}  # filtered out
        return None

    monkeypatch.setattr(conn, "_get", fake_get)
    posts = asyncio.run(conn.fetch_posts(limit=10))
    assert [p.post_id for p in posts] == ["1", "2"]
    google = posts[0]
    assert google.platform == "hackernews"
    assert google.likes == 295 and google.comments == 213
    assert google.shares is None  # HN exposes no reshare count — stays None
    assert google.author_id == "person1"
    assert google.url == "https://example.com/a"
    ask = posts[1]
    assert ask.url == "https://news.ycombinator.com/item?id=2"  # HN fallback URL


def test_hn_metrics_refetch_returns_snapshot(monkeypatch):
    conn = HackerNewsConnector()

    async def fake_get(path):
        assert path == "/item/99.json"
        return {"id": 99, "score": 500, "descendants": 120,
                "time": 1788000000, "by": "x", "title": "t"}

    monkeypatch.setattr(conn, "_get", fake_get)
    snaps = asyncio.run(conn.fetch_post_metrics("99"))
    assert len(snaps) == 1
    assert snaps[0].likes == 500 and snaps[0].comments == 120
    assert snaps[0].timestamp.tzinfo is not None


def test_hn_api_failure_raises_connector_error(monkeypatch):
    conn = HackerNewsConnector()

    async def fail(path):
        raise ConnectorError("api down", kind="unavailable")

    monkeypatch.setattr(conn, "_get", fail)
    with pytest.raises(ConnectorError):
        asyncio.run(conn.fetch_posts(limit=5))


# ----------------------------------------------------------------- mastodon
def test_mastodon_post_maps_real_engagement(monkeypatch):
    from app.core.config import settings

    conn = MastodonConnector()
    monkeypatch.setattr(settings, "MASTODON_INSTANCES",
                        "mastodon.world,techhub.social,universeodon.com")

    async def fake_get(instance, path, params=None):
        assert path == "/api/v1/timelines/public"
        if instance != "mastodon.world":
            return []  # other instances have nothing this cycle
        return [
            {"id": "1172050276245", "visibility": "public",
             "content": "<p>Trump threatens more strikes as toll rises</p>",
             "created_at": "2026-09-03T10:00:00.000Z",
             "language": "en",
             "account": {"acct": "user@instance", "display_name": "User",
                         "followers_count": 1000},
             "favourites_count": 12, "replies_count": 3, "reblogs_count": 5,
             "url": f"https://{instance}/@user/1172050276245"},
            {"id": "1172050276246", "visibility": "unlisted",
             "content": "<p>unlisted but public-facing post content here</p>",
             "created_at": "2026-09-03T10:01:00.000Z", "account": {"acct": "b"},
             "favourites_count": 1, "replies_count": 0, "reblogs_count": 2,
             "url": "https://mastodon.world/@b/1172050276246"},
            {"id": "1172050276247", "visibility": "private",
             "content": "<p>private post must never be ingested</p>",
             "created_at": "2026-09-03T10:02:00.000Z", "account": {"acct": "c"},
             "favourites_count": 1, "replies_count": 0, "reblogs_count": 0,
             "url": "https://mastodon.world/@c/1172050276247"},
            {"id": "1172050276248", "reblog": {"id": "1"}, "content": "",
             "created_at": "2026-09-03T10:03:00.000Z", "account": {"acct": "d"},
             "url": "https://mastodon.world/@d/1172050276248"},  # boost wrapper
        ]

    monkeypatch.setattr(conn, "_get", fake_get)
    posts = asyncio.run(conn.fetch_posts(limit=10))
    # public + unlisted kept; private and boost-wrapper dropped
    assert [p.post_id for p in posts] == [
        "mastodon.world:1172050276245", "mastodon.world:1172050276246"]
    post = posts[0]
    assert post.shares == 5 and post.likes == 12 and post.comments == 3
    assert post.content == "Trump threatens more strikes as toll rises"
    assert post.author_id == "user@instance"


def test_mastodon_metrics_refetch(monkeypatch):
    conn = MastodonConnector()

    async def fake_get(instance, path, params=None):
        assert path == "/api/v1/statuses/555"
        return {"id": "555", "favourites_count": 42, "replies_count": 7,
                "reblogs_count": 9}

    monkeypatch.setattr(conn, "_get", fake_get)
    snaps = asyncio.run(conn.fetch_post_metrics("mastodon.world:555"))
    assert snaps[0].likes == 42 and snaps[0].shares == 9 and snaps[0].comments == 7
    # malformed external id — no snapshot, no crash
    assert asyncio.run(conn.fetch_post_metrics("nonsense")) == []


def test_mastodon_instances_parsed_from_settings():
    conn = MastodonConnector()
    assert isinstance(conn.instances, list)


# ------------------------------------------------- registry fallback wiring
def test_registry_uses_harvesters_without_credentials():
    from app.connectors import get_connector

    for platform in ("x", "reddit", "instagram", "facebook", "linkedin"):
        conn = get_connector(platform)
        assert type(conn).__name__ == "WebHarvestConnector", platform
        assert conn.supports_discovery is True

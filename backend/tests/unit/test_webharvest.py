"""Unit tests for the keyless web-search harvest connectors (no network).

The sidecar call is monkeypatched — these tests verify URL matching, honest
metric extraction, publish-time parsing and normalization only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.connectors.base import ConnectorError
from app.connectors.webharvest import (
    TOPICS,
    WebHarvestConnector,
    clean_title,
    match_post_url,
    parse_engagement,
    parse_published_at,
)


# ------------------------------------------------------------ URL matching
def test_x_status_url_matches():
    m = match_post_url("x", "https://twitter.com/AltNews/status/1942168249946280088")
    assert m and m.external_id == "1942168249946280088" and m.author_id == "@AltNews"
    m2 = match_post_url("x", "https://x.com/DDNewslive/status/2056610998275215542?ref=twsrc")
    assert m2 and m2.external_id == "2056610998275215542"


def test_x_profile_and_homepage_rejected():
    assert match_post_url("x", "https://x.com/NBCNews") is None
    assert match_post_url("x", "https://x.com") is None
    assert match_post_url("x", "https://nymag.com/status/x") is None


def test_reddit_comments_url_matches():
    m = match_post_url("reddit", "https://www.reddit.com/r/Vaccine/comments/1w4huyh/request/")
    assert m and m.external_id == "1w4huyh" and m.author_id == "r/Vaccine"


def test_reddit_subreddit_listing_rejected():
    assert match_post_url("reddit", "https://www.reddit.com/r/worldnews/") is None


def test_instagram_post_and_reel_match():
    m = match_post_url("instagram", "https://www.instagram.com/p/DWH4_iUFP67/")
    assert m and m.external_id == "DWH4_iUFP67"
    m2 = match_post_url("instagram", "https://instagram.com/reel/DXrK8sljHEb")
    assert m2 and m2.external_id == "DXrK8sljHEb"


def test_instagram_profile_rejected():
    assert match_post_url("instagram", "https://www.instagram.com/nasa/") is None


def test_facebook_post_variants_match():
    m = match_post_url("facebook", "https://www.facebook.com/InstitutoMix/posts/7215ee9c/1450446")
    assert m and m.external_id == "7215ee9c" and m.author_id == "InstitutoMix"
    m2 = match_post_url("facebook", "https://www.facebook.com/share/p/abcDEF123/")
    assert m2 and m2.external_id == "abcDEF123"
    m3 = match_post_url("facebook", "https://www.facebook.com/story.php?story_fbid=1234&id=99")
    assert m3 and m3.external_id == "1234"
    m4 = match_post_url("facebook", "https://www.facebook.com/reel/998877")
    assert m4 and m4.external_id == "998877"


def test_facebook_help_page_rejected():
    assert match_post_url("facebook", "https://www.facebook.com/help/188118808357379") is None


def test_linkedin_post_matches():
    m = match_post_url("linkedin", "https://www.linkedin.com/posts/pramod-rana-626878bb_how-to-science-activity-123")
    assert m and m.author_id == "pramod" and m.external_id.startswith("pramod-rana")


# --------------------------------------------------------- metric extraction
def test_parse_engagement_counts_mapped():
    found = parse_engagement("QuantumAI scam alert — 62 votes, 316 comments .")
    assert found == {"likes": 62, "comments": 316}
    assert parse_engagement("9,509 likes · 312 comments")["likes"] == 9509
    assert parse_engagement("1.2K Likes 342 Comments 88 Reposts") == {
        "likes": 1200, "comments": 342, "shares": 88}


def test_parse_engagement_absent_is_empty():
    assert parse_engagement("no numbers in this snippet at all") == {}
    assert parse_engagement("") == {}


# ------------------------------------------------------------ publish times
def test_parse_published_relative_minutes():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    ts = parse_published_at(text="Breaking: shot fired 47m", fallback=now)
    assert (now - ts) == timedelta(minutes=47)


def test_parse_published_hours_ago_phrase():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    ts = parse_published_at(text="posted 2 hours ago", fallback=now)
    assert (now - ts) == timedelta(hours=2)


def test_parse_published_iso_date_field():
    ts = parse_published_at(date_field="2026-09-01T08:30", text="")
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 9, 1, 8, 30)
    assert ts.tzinfo is not None


def test_parse_published_falls_back_to_harvest_time():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    assert parse_published_at(text="nothing usable", fallback=now) == now


# ---------------------------------------------------------------- titles
def test_clean_title_strips_platform_suffix_and_grabs_handle():
    title, author = clean_title("NBC News (@NBCNews) / X")
    assert title == "NBC News" and author == "@NBCNews"
    title2, author2 = clean_title("RBI has not asked banks — Reddit")
    assert author2 is None and "Reddit" not in title2


# ------------------------------------------------------------ fetch_posts
def test_fetch_posts_normalizes_and_dedupes(monkeypatch):
    conn = WebHarvestConnector("reddit")

    async def fake_search(query):
        return [
            {"url": "https://www.reddit.com/r/science/comments/abc123/cool_study/",
             "name": "Cool study — Reddit",
             "snippet": "62 votes, 316 comments . Researchers found something."},
            {"url": "https://www.reddit.com/r/science/comments/abc123/cool_study/",
             "name": "dup", "snippet": "x"},
            {"url": "https://www.reddit.com/r/science/",
             "name": "r/science", "snippet": "the front page of the internet"},
            {"url": "https://www.reddit.com/r/news/comments/def456/other/",
             "name": "Other post", "snippet": "Some real text long enough to keep."},
        ]

    monkeypatch.setattr(conn, "_search", fake_search)
    posts = asyncio.run(conn.fetch_posts(limit=10))
    assert len(posts) == 2
    first = posts[0]
    assert first.platform == "reddit"
    assert first.post_id == "abc123"
    assert first.author_id == "r/science"
    assert first.likes == 62 and first.comments == 316 and first.shares is None
    assert first.is_demo is False
    assert conn.last_harvest_new == 2


def test_fetch_posts_rotates_topics_and_records_harvest(monkeypatch):
    conn = WebHarvestConnector("x")

    queries: list[str] = []

    async def fake_search(query):
        queries.append(query)
        return []

    monkeypatch.setattr(conn, "_search", fake_search)
    asyncio.run(conn.fetch_posts(limit=6))
    assert queries, "expected at least one search query"
    assert all("site:" in q or "twitter" in q or "x.com" in q for q in queries)

    before = conn._topic_index
    asyncio.run(conn.fetch_posts(limit=6))
    assert conn._topic_index != before or len(TOPICS) == 1
    assert conn.last_harvest_at is not None


def test_search_failure_raises_connector_error(monkeypatch):
    import httpx

    conn = WebHarvestConnector("facebook")

    class FailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("sidecar down")

    monkeypatch.setattr(httpx, "AsyncClient", FailClient)
    with pytest.raises(ConnectorError):
        asyncio.run(conn._search("anything"))


def test_invalid_platform_rejected():
    with pytest.raises(ValueError):
        WebHarvestConnector("mastodon")

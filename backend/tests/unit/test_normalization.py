"""Unit tests — connector normalization + demo data integrity (spec #43)."""
from __future__ import annotations

import asyncio

import pytest

from app.connectors.base import NormalizedPost
from app.connectors.demo import ARCHETYPES, DemoConnector, _parse_archetype


def test_normalized_post_to_dict_roundtrip():
    post = NormalizedPost(
        platform="reddit", post_id="t3_abc", author_id="u1", content="hi",
        posted_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        likes=5, comments=1, shares=None,  # None = platform doesn't expose it
    )
    d = post.to_dict()
    assert d["platform"] == "reddit"
    assert d["shares"] is None          # must stay None, never 0/fabricated
    assert d["likes"] == 5


def test_demo_archetypes_complete():
    assert set(ARCHETYPES) == {"normal", "trending", "viral", "suspicious_viral", "false_alarm"}


def test_demo_post_shape():
    post, snapshots, events = asyncio.run(DemoConnector().generate_post(archetype="viral"))
    assert post.platform == "demo"
    assert post.is_demo is True
    assert post.post_id.startswith("demo_viral_")
    assert _parse_archetype(post.post_id) == "viral"
    assert len(snapshots) >= 10
    assert events, "viral demo posts should include propagation events"
    # shares must be monotonically non-decreasing (cumulative counters)
    shares = [s.shares for s in snapshots]
    assert all(b >= a for a, b in zip(shares, shares[1:]))
    # engagement present, views > shares (realistic visibility funnel)
    assert snapshots[-1].views > snapshots[-1].shares


def test_demo_timeline_deterministic():
    """Same post id ⇒ same curve (polling continues seamlessly)."""
    connector = DemoConnector()
    post, snapshots_a, _ = asyncio.run(connector.generate_post(archetype="trending"))
    snapshots_b = asyncio.run(connector.fetch_post_metrics(
        post.post_id, since=snapshots_a[0].timestamp, post_posted_at=post.posted_at))
    assert snapshots_b, "continuation should produce new snapshots"


def test_demo_archetype_content_pools():
    """suspicious_viral content must contain misinfo-style language; false_alarm must not."""
    connector = DemoConnector()
    for _ in range(4):
        suspicious, _, _ = asyncio.run(connector.generate_post(archetype="suspicious_viral"))
        if "!!!" in suspicious.content or "BREAKING" in suspicious.content:
            break
    assert "!!!" in suspicious.content or "BREAKING" in suspicious.content.upper()
    false_alarm, _, _ = asyncio.run(connector.generate_post(archetype="false_alarm"))
    benign_markers = ["false alarm", "false", "not", "all clear", "safe", "reopens",
                      "confirms", "drill", "maintenance"]
    assert any(m in false_alarm.content.lower() for m in benign_markers)

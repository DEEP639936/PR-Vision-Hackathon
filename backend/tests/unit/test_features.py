"""Unit tests — feature engineering mathematics (spec #43).

The temporal maths must be exact: velocity, acceleration, engagement,
unique-sharer growth, propagation topology, NLP signals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ml.feature_engineering import (
    build_feature_vector,
    engagement_acceleration,
    engagement_velocity,
    nlp_features,
    per_minute,
    propagation_features,
    share_acceleration,
    share_velocity,
    unique_sharer_features,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def snap(minutes_after: float, *, shares=None, likes=None, comments=None, views=None, unique_sharers=None):
    return {
        "timestamp": T0 + timedelta(minutes=minutes_after),
        "shares": shares, "likes": likes, "comments": comments,
        "views": views, "unique_sharers": unique_sharers,
        "followers": 1000,
    }


# ------------------------------------------------------------------- rates
def test_per_minute_basic():
    assert per_minute(30, 60) == pytest.approx(30.0)          # 30 units in 60s
    assert per_minute(10, 300) == pytest.approx(2.0)          # 10 in 5 min
    assert per_minute(0, 100) == 0.0
    assert per_minute(5, 0) is None                            # invalid window
    assert per_minute(None, 100) is None


def test_share_velocity_linear_growth():
    history = [snap(0, shares=0), snap(5, shares=50), snap(10, shares=110)]
    v = share_velocity(history, T0 + timedelta(minutes=10), window_seconds=600)
    assert v == pytest.approx(11.0)  # 110 shares / 10 min


def test_share_velocity_requires_history():
    history = [snap(0, shares=10)]
    assert share_velocity(history, T0 + timedelta(minutes=1)) is None


def test_share_acceleration_increasing():
    # 5-min cadence: velocity 10/min over [5,10] then 15/min over [10,15]
    history = [snap(0, shares=0), snap(5, shares=25), snap(10, shares=75), snap(15, shares=150)]
    a = share_acceleration(history, T0 + timedelta(minutes=15), window_seconds=300)
    assert a is not None and a > 0


def test_share_acceleration_insufficient_history_is_none():
    history = [snap(0, shares=0), snap(5, shares=25)]
    assert share_acceleration(history, T0 + timedelta(minutes=5), window_seconds=300) is None


def test_engagement_velocity_counts_likes_comments_shares():
    history = [
        snap(0, shares=10, likes=40, comments=5),
        snap(5, shares=20, likes=80, comments=15),
    ]
    v = engagement_velocity(history, T0 + timedelta(minutes=5), window_seconds=300)
    # Δ(40+5+10 → 80+15+20) = 60 over 5 minutes
    assert v == pytest.approx(12.0)


def test_engagement_acceleration_sign():
    fast = [snap(0, shares=0, likes=0, comments=0), snap(5, shares=10, likes=40, comments=10)]
    slow = fast + [snap(10, shares=12, likes=44, comments=11), snap(15, shares=13, likes=46, comments=12)]
    assert engagement_acceleration(slow, T0 + timedelta(minutes=15)) < 0


def test_unique_sharer_growth():
    history = [snap(0, unique_sharers=10), snap(5, unique_sharers=18)]
    feats = unique_sharer_features(history, T0 + timedelta(minutes=5))
    assert feats["new_unique_sharers"] == 8
    assert feats["unique_sharer_growth_rate"] == pytest.approx(0.8)


def test_propagation_features_topology():
    events = [
        {"source_user_id": "a", "target_user_id": "b", "depth": 1, "timestamp": T0 + timedelta(seconds=30)},
        {"source_user_id": "b", "target_user_id": "c", "depth": 2, "timestamp": T0 + timedelta(seconds=60)},
        {"source_user_id": "a", "target_user_id": "d", "depth": 1, "timestamp": T0 + timedelta(seconds=90)},
    ]
    feats = propagation_features(events, T0 + timedelta(minutes=5))
    assert feats["propagation_depth"] == 2
    assert feats["cascade_size"] == 3
    assert feats["branching_factor"] == pytest.approx(0.5)  # 1 secondary / 2 primary
    assert feats["propagation_breadth"] == 3
    assert feats["avg_time_between_shares"] == pytest.approx(30.0)


def test_propagation_empty_when_no_events():
    assert propagation_features([], T0)["cascade_size"] is None


def test_nlp_flags_misinfo_style():
    bad = nlp_features("BREAKING!!! doctors don't want you to know this MIRACLE cure — SHARE NOW!!!")
    good = nlp_features("Our beautiful community garden won the award — proud and happy volunteers enjoyed a wonderful day.")
    assert bad["claim_score"] > good["claim_score"]
    assert bad["urgency_score"] > good["urgency_score"]
    assert bad["sensational_score"] > good["sensational_score"]
    assert bad["exclamation_count"] >= 3
    assert good["sentiment_score"] > bad["sentiment_score"] >= 0.0


def test_nlp_counters():
    f = nlp_features("Run now! #marathon https://example.com")
    assert f["hashtag_count"] == 1
    assert f["url_present"] is True
    assert f["question_count"] == 0
    assert f["word_count"] == 4


# ------------------------------------------------------------- master builder
def test_build_feature_vector_strictly_causal():
    """Future snapshots must not influence features at t."""
    past = [snap(0, shares=10, likes=20, comments=2, views=200, unique_sharers=5),
            snap(5, shares=20, likes=30, comments=3, views=300, unique_sharers=9)]
    future = [snap(60, shares=5000, likes=9999, comments=999, views=99999, unique_sharers=900)]
    as_of = T0 + timedelta(minutes=5)

    with_future = build_feature_vector(
        post_posted_at=T0, snapshot_history=past + future,
        propagation_events=[], content="hello", as_of=as_of)
    without_future = build_feature_vector(
        post_posted_at=T0, snapshot_history=past,
        propagation_events=[], content="hello", as_of=as_of)

    assert with_future["current_shares"] == without_future["current_shares"] == 20
    assert with_future["share_velocity"] == without_future["share_velocity"]


def test_build_feature_vector_temporal_fields():
    feats = build_feature_vector(
        post_posted_at=T0, snapshot_history=[snap(0, shares=1)],
        propagation_events=[], content="x", as_of=T0)
    # 2026-01-01 is a Thursday
    assert feats["day_of_week"] == 3
    assert feats["is_weekend"] is False
    assert feats["time_since_post"] == 0


def test_missing_metrics_become_none_not_zero():
    feats = build_feature_vector(
        post_posted_at=T0, snapshot_history=[snap(0, shares=5)],  # views missing
        propagation_events=[], content="x", as_of=T0)
    assert feats["current_views"] is None

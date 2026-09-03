"""Feature service — builds and persists causal feature snapshots from DB data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import FeatureSnapshot, Post
from app.db.repositories import (
    FeatureSnapshotRepository,
    MetricSnapshotRepository,
    PropagationRepository,
)
from app.ml.feature_engineering import build_feature_vector

logger = get_logger("prvision.services.feature")


class FeatureService:
    @staticmethod
    def _load_history(db: Session, post_id: int) -> tuple[list[dict], list[dict]]:
        snaps = MetricSnapshotRepository.history(db, post_id)
        snapshot_history = [
            {
                "timestamp": s.timestamp,
                "likes": s.likes,
                "comments": s.comments,
                "shares": s.shares,
                "views": s.views,
                "followers": s.followers,
                "unique_sharers": s.unique_sharers,
            }
            for s in snaps
        ]
        events = [
            {
                "source_user_id": e.source_user_id,
                "target_user_id": e.target_user_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "time_since_original_post": e.time_since_original_post,
                "depth": e.depth,
            }
            for e in PropagationRepository.for_post(db, post_id)
        ]
        return snapshot_history, events

    @classmethod
    def compute_latest(cls, db: Session, post: Post, *, as_of: datetime | None = None) -> dict[str, Any]:
        """Compute the causal feature vector at `as_of` (default: newest snapshot)."""
        snapshot_history, events = cls._load_history(db, post.id)
        return build_feature_vector(
            post_posted_at=post.posted_at,
            snapshot_history=snapshot_history,
            propagation_events=events,
            content=post.content,
            as_of=as_of,
        )

    @classmethod
    def compute_and_persist(cls, db: Session, post: Post, *, as_of: datetime | None = None) -> dict[str, Any]:
        """Compute the current feature vector and store it as a FeatureSnapshot.

        Returns the feature dict (includes `timestamp`).
        """
        features = cls.compute_latest(db, post, as_of=as_of)
        if not features.get("current_shares") and not features.get("share_velocity"):
            # Nothing measurable yet (e.g. single snapshot) — still store basics.
            pass

        ts = features.get("timestamp") or datetime.now(timezone.utc)
        storable = {k: v for k, v in features.items()
                    if k != "timestamp" and k in FeatureSnapshot.__table__.columns}
        FeatureSnapshotRepository.add(db, post.id, ts, **storable)
        return features

    @classmethod
    def backfill_features(cls, db: Session, post: Post, *, every_n: int = 1) -> int:
        """Replay historical snapshots and persist causal feature vectors at each.

        For each historical metric snapshot time t, computes features using ONLY
        data available at ≤ t (no leakage by construction) and stores the row.
        This reproduces what the live system would have recorded, enabling
        model training immediately after a backfilled ingestion (e.g. demo).

        Returns the number of feature snapshots stored.
        """
        snapshot_history, events = cls._load_history(db, post.id)
        if len(snapshot_history) < 2:
            return 0
        stored = 0
        for idx, snap in enumerate(snapshot_history):
            if idx % max(1, every_n) != 0:
                continue
            t = snap["timestamp"]
            features = build_feature_vector(
                post_posted_at=post.posted_at,
                snapshot_history=snapshot_history[: idx + 1],  # strictly causal slice
                propagation_events=events,
                content=post.content,
                as_of=t,
            )
            storable = {k: v for k, v in features.items()
                        if k != "timestamp" and k in FeatureSnapshot.__table__.columns}
            FeatureSnapshotRepository.add(db, post.id, t, **storable)
            stored += 1
        return stored

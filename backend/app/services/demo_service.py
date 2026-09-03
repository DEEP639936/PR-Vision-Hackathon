"""Demo service — generates realistic demo posts through the REAL pipeline.

DemoConnector → NormalizedPost (+ backfilled snapshots + propagation events)
    → MySQL → Feature engineering → ML → Intervention score

POST /api/demo/generate is idempotent per external id; each generated post is
labelled is_demo=True so the dashboard can badge it honestly.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.connectors.demo import ARCHETYPES, DemoConnector
from app.core.logging import get_logger
from app.db.repositories import MetricSnapshotRepository, PostRepository
from app.services.feature_service import FeatureService
from app.services.ingestion_service import IngestionService
from app.services.prediction_service import PredictionService

logger = get_logger("prvision.services.demo")

ARCHETYPE_DESCRIPTIONS = {
    "normal": "Slow, organic propagation — low spread risk",
    "trending": "Moderate accelerating growth",
    "viral": "Rapid propagation of benign content",
    "suspicious_viral": "Rapid propagation + misinformation-style content",
    "false_alarm": "Rapid propagation, alarming framing, but benign",
}


class DemoService:
    @staticmethod
    async def generate_posts(
        db: Session,
        *,
        num_posts: int = 5,
        archetypes: list[str] | None = None,
        score: bool = True,
    ) -> list[dict[str, Any]]:
        """Create demo posts with full backfilled history, then score them."""
        connector = DemoConnector()
        archetypes = [a for a in (archetypes or ARCHETYPES) if a in ARCHETYPES] or list(ARCHETYPES)

        created: list[dict[str, Any]] = []
        for i in range(max(1, min(num_posts, 20))):
            archetype = archetypes[i % len(archetypes)]
            post_payload, snapshots, events = await connector.generate_post(archetype=archetype)
            post = await IngestionService.ingest_new_post(db, connector, post_payload)
            # The initial snapshot at posted_at already exists (ingest_new_post);
            # keep only strictly-later backfill snapshots to avoid UNIQUE clashes.
            fresh = [s for s in snapshots if s.timestamp > post.posted_at]
            if fresh:
                MetricSnapshotRepository.bulk_add(
                    db, post.id,
                    [{"timestamp": s.timestamp, "likes": s.likes, "comments": s.comments,
                      "shares": s.shares, "views": s.views, "followers": s.followers,
                      "unique_sharers": s.unique_sharers} for s in fresh])
                item_snapshot_count = len(fresh)
            else:
                item_snapshot_count = 0
            await IngestionService.store_propagation(db, post, events)
            # Replay causal features over the backfilled history so the model
            # has immediate training data (same engineering as live scoring).
            backfilled = FeatureService.backfill_features(db, post)
            logger.info("Demo post %s (%s): %d snapshots, %d propagation events, %d feature rows",
                        post.id, archetype, item_snapshot_count + 1, len(events), backfilled)
            created.append({"post_id": post.id, "external_post_id": post.external_post_id,
                            "archetype": archetype, "snapshots": item_snapshot_count,
                            "feature_rows": backfilled,
                            "propagation_events": len(events)})

        if score:
            for item in created:
                post = PostRepository.get_by_id(db, item["post_id"])
                if post:
                    try:
                        PredictionService.score_post(db, post)
                    except Exception:
                        logger.exception("Demo scoring failed for post %s", post.id)
        return created

#!/usr/bin/env python3
"""Seed the PR•VISION database with realistic demo data.

Runs the DemoConnector → normalizer → DB pipeline exactly like real ingestion,
then scores every post. Safe to re-run (idempotent per external id).

Usage (from project root or backend/):
    python scripts/seed_demo_data.py                 # 10 posts (2 per archetype)
    python scripts/seed_demo_data.py --posts 15      # more posts
    python scripts/seed_demo_data.py --archetypes viral suspicious_viral
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.logging import configure_logging, get_logger  # noqa: E402

configure_logging("INFO")
logger = get_logger("prvision.scripts.seed")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed PR•VISION with demo data")
    parser.add_argument("--posts", type=int, default=10, help="total demo posts to create")
    parser.add_argument("--archetypes", nargs="*", default=None,
                        help="subset of: normal trending viral suspicious_viral false_alarm")
    args = parser.parse_args()

    # Ensure schema exists even without alembic (dev convenience).
    from app.db.database import engine, session_scope
    from app.db.models import Base
    Base.metadata.create_all(bind=engine)

    from app.services.demo_service import DemoService
    from app.services.prediction_service import PredictionService
    from app.db.repositories import PostRepository

    with session_scope() as db:
        existing, _ = PostRepository.list_posts(db, limit=1)
        if existing:
            logger.info("Database already contains posts — skipping seeding "
                        "(delete the DB file or drop schema to reseed).")
            return 0

        created = await DemoService.generate_posts(
            db, num_posts=args.posts, archetypes=args.archetypes, score=False)
        logger.info("Created %d demo posts; scoring via full ML pipeline…", len(created))

        results = PredictionService.score_all_active(db, limit=500)

    logger.info("Seed complete: %d posts scored", len(results))
    for r in results[:12]:
        logger.info("  post %-4s [%s] priority=%5.1f misinfo=%.2f velocity=%s",
                    r["post_id"], r["priority_label"], r["intervention_priority"],
                    r["misinformation_risk"], r["share_velocity"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

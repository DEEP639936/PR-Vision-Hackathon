"""Repository layer — all database queries live here, keeping services lean.

Repositories are grouped by aggregate and expose only what the API/services
actually need. Queries are paginated/index-aware; aggregates are computed in
SQL wherever possible.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    DataSourceStatus,
    FeatureSnapshot,
    InterventionScore,
    MetricSnapshot,
    MisinformationScore,
    Post,
    Prediction,
    PropagationEvent,
)

PriorityLabel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


# --------------------------------------------------------------------------- posts
class PostRepository:
    @staticmethod
    def get_by_id(db: Session, post_id: int) -> Post | None:
        return db.get(Post, post_id)

    @staticmethod
    def get_by_external_id(db: Session, platform: str, external_post_id: str) -> Post | None:
        stmt = select(Post).where(Post.platform == platform, Post.external_post_id == external_post_id)
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def create(db: Session, **fields) -> Post:
        post = Post(**fields)
        db.add(post)
        db.flush()
        return post

    @staticmethod
    def list_posts(
        db: Session,
        *,
        platform: str | None = None,
        search: str | None = None,
        posted_after: datetime | None = None,
        posted_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Post], int]:
        stmt = select(Post)
        count_stmt = select(func.count(Post.id))
        if platform:
            stmt = stmt.where(Post.platform == platform)
            count_stmt = count_stmt.where(Post.platform == platform)
        if search:
            like = f"%{search}%"
            cond = Post.content.ilike(like) | Post.author_id.ilike(like)
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if posted_after:
            stmt = stmt.where(Post.posted_at >= posted_after)
            count_stmt = count_stmt.where(Post.posted_at >= posted_after)
        if posted_before:
            stmt = stmt.where(Post.posted_at <= posted_before)
            count_stmt = count_stmt.where(Post.posted_at <= posted_before)

        total = db.execute(count_stmt).scalar_one()
        rows = db.execute(stmt.order_by(Post.posted_at.desc()).limit(limit).offset(offset)).scalars().all()
        return rows, int(total)

    @staticmethod
    def count_all(db: Session) -> int:
        return int(db.execute(select(func.count(Post.id))).scalar_one())


# ----------------------------------------------------------------- metric snapshots
class MetricSnapshotRepository:
    @staticmethod
    def add(db: Session, post_id: int, timestamp: datetime, **metrics) -> MetricSnapshot:
        snap = MetricSnapshot(post_id=post_id, timestamp=timestamp, **metrics)
        db.add(snap)
        db.flush()
        return snap

    @staticmethod
    def latest(db: Session, post_id: int) -> MetricSnapshot | None:
        stmt = (
            select(MetricSnapshot)
            .where(MetricSnapshot.post_id == post_id)
            .order_by(MetricSnapshot.timestamp.desc())
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def history(db: Session, post_id: int, since: datetime | None = None) -> Sequence[MetricSnapshot]:
        stmt = select(MetricSnapshot).where(MetricSnapshot.post_id == post_id)
        if since:
            stmt = stmt.where(MetricSnapshot.timestamp >= since)
        return db.execute(stmt.order_by(MetricSnapshot.timestamp.asc())).scalars().all()

    @staticmethod
    def series(
        db: Session, post_id: int, since: datetime | None = None
    ) -> list[dict]:
        """Lightweight time-series payload for charts."""
        rows = MetricSnapshotRepository.history(db, post_id, since)
        return [
            {
                "timestamp": r.timestamp,
                "likes": r.likes,
                "comments": r.comments,
                "shares": r.shares,
                "views": r.views,
                "unique_sharers": r.unique_sharers,
            }
            for r in rows
        ]

    @staticmethod
    def bulk_add(db: Session, post_id: int, snapshots: Iterable[dict]) -> int:
        """Batch insert (single flush) — used by demo/backfill ingestion."""
        objs = [MetricSnapshot(post_id=post_id, **s) for s in snapshots]
        db.bulk_save_objects(objs)
        db.flush()
        return len(objs)


# --------------------------------------------------------------- propagation events
class PropagationRepository:
    @staticmethod
    def bulk_add(db: Session, post_id: int, events: Iterable[dict]) -> int:
        objs = [PropagationEvent(post_id=post_id, **e) for e in events]
        db.bulk_save_objects(objs)
        db.flush()
        return len(objs)

    @staticmethod
    def for_post(db: Session, post_id: int, limit: int = 500) -> Sequence[PropagationEvent]:
        stmt = (
            select(PropagationEvent)
            .where(PropagationEvent.post_id == post_id)
            .order_by(PropagationEvent.timestamp.asc())
            .limit(limit)
        )
        return db.execute(stmt).scalars().all()


# --------------------------------------------------------------- feature snapshots
class FeatureSnapshotRepository:
    @staticmethod
    def exists_at(db: Session, post_id: int, timestamp: datetime) -> bool:
        stmt = (
            select(FeatureSnapshot.id)
            .where(FeatureSnapshot.post_id == post_id, FeatureSnapshot.timestamp == timestamp)
            .limit(1)
        )
        return db.execute(stmt).first() is not None

    @staticmethod
    def add(db: Session, post_id: int, timestamp: datetime, **features) -> FeatureSnapshot | None:
        """Insert unless a snapshot for (post_id, timestamp) already exists."""
        if FeatureSnapshotRepository.exists_at(db, post_id, timestamp):
            return None
        snap = FeatureSnapshot(post_id=post_id, timestamp=timestamp, **features)
        db.add(snap)
        db.flush()
        return snap

    @staticmethod
    def latest(db: Session, post_id: int) -> FeatureSnapshot | None:
        stmt = (
            select(FeatureSnapshot)
            .where(FeatureSnapshot.post_id == post_id)
            .order_by(FeatureSnapshot.timestamp.desc())
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def history(db: Session, post_id: int, since: datetime | None = None) -> Sequence[FeatureSnapshot]:
        stmt = select(FeatureSnapshot).where(FeatureSnapshot.post_id == post_id)
        if since:
            stmt = stmt.where(FeatureSnapshot.timestamp >= since)
        return db.execute(stmt.order_by(FeatureSnapshot.timestamp.asc())).scalars().all()

    @staticmethod
    def all_features(db: Session, limit: int = 200000) -> list[dict]:
        """Flattened feature rows for training (feature dicts + targets computed later)."""
        stmt = select(FeatureSnapshot).order_by(FeatureSnapshot.timestamp.asc()).limit(limit)
        rows = db.execute(stmt).scalars().all()
        out = []
        for r in rows:
            d = {c.name: getattr(r, c.name) for c in FeatureSnapshot.__table__.columns}
            out.append(d)
        return out


# --------------------------------------------------------------------- predictions
class PredictionRepository:
    @staticmethod
    def add(db: Session, **fields) -> Prediction:
        pred = Prediction(**fields)
        db.add(pred)
        db.flush()
        return pred

    @staticmethod
    def latest_for_post(db: Session, post_id: int, horizon_minutes: int | None = None) -> Prediction | None:
        stmt = select(Prediction).where(Prediction.post_id == post_id)
        if horizon_minutes:
            stmt = stmt.where(Prediction.horizon_minutes == horizon_minutes)
        stmt = stmt.order_by(Prediction.prediction_timestamp.desc()).limit(1)
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def for_post(db: Session, post_id: int) -> Sequence[Prediction]:
        stmt = (
            select(Prediction)
            .where(Prediction.post_id == post_id)
            .order_by(Prediction.prediction_timestamp.desc())
            .limit(50)
        )
        return db.execute(stmt).scalars().all()

    @staticmethod
    def sum_predicted_additional(db: Session, horizon_minutes: int = 60) -> float:
        stmt = select(func.coalesce(func.sum(Prediction.predicted_additional_shares), 0.0)).where(
            Prediction.horizon_minutes == horizon_minutes,
            Prediction.prediction_timestamp >= datetime.now(timezone.utc) - timedelta(hours=6),
        )
        return float(db.execute(stmt).scalar_one())


# -------------------------------------------------------------- misinformation scores
class MisinformationRepository:
    @staticmethod
    def add(db: Session, **fields) -> MisinformationScore:
        obj = MisinformationScore(**fields)
        db.add(obj)
        db.flush()
        return obj

    @staticmethod
    def latest_for_post(db: Session, post_id: int) -> MisinformationScore | None:
        stmt = (
            select(MisinformationScore)
            .where(MisinformationScore.post_id == post_id)
            .order_by(MisinformationScore.timestamp.desc())
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def avg_risk(db: Session) -> float | None:
        """Mean risk across the LATEST score of each post (rows accumulate
        per refresh cycle — averaging all rows would bias over time)."""
        latest_ids = (
            select(func.max(MisinformationScore.id).label("max_id"))
            .group_by(MisinformationScore.post_id)
            .subquery()
        )
        stmt = select(func.avg(MisinformationScore.risk_score)).where(
            MisinformationScore.id.in_(select(latest_ids.c.max_id)))
        val = db.execute(stmt).scalar_one()
        return float(val) if val is not None else None


# -------------------------------------------------------------- intervention scores
class InterventionRepository:
    @staticmethod
    def add(db: Session, **fields) -> InterventionScore:
        obj = InterventionScore(**fields)
        db.add(obj)
        db.flush()
        return obj

    @staticmethod
    def latest_for_post(db: Session, post_id: int) -> InterventionScore | None:
        stmt = (
            select(InterventionScore)
            .where(InterventionScore.post_id == post_id)
            .order_by(InterventionScore.timestamp.desc())
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def high_priority(
        db: Session,
        *,
        min_priority: float = 50.0,
        platform: str | None = None,
        label: str | None = None,
        limit: int = 25,
    ) -> list[tuple[InterventionScore, Post]]:
        stmt = (
            select(InterventionScore, Post)
            .join(Post, InterventionScore.post_id == Post.id)
            .where(InterventionScore.intervention_priority >= min_priority)
        )
        if platform:
            stmt = stmt.where(Post.platform == platform)
        if label:
            stmt = stmt.where(InterventionScore.priority_label == label.upper())

        # Latest score per post: join against max(id) per post.
        latest_ids = (
            select(func.max(InterventionScore.id).label("max_id"))
            .group_by(InterventionScore.post_id)
            .subquery()
        )
        stmt = stmt.where(InterventionScore.id.in_(select(latest_ids.c.max_id)))
        stmt = stmt.order_by(InterventionScore.intervention_priority.desc()).limit(limit)
        return [tuple(r) for r in db.execute(stmt).all()]

    @staticmethod
    def label_counts(db: Session) -> dict[str, int]:
        """Count only the LATEST score per post (rows accumulate per refresh
        cycle, so counting every row would inflate the dashboard KPIs)."""
        latest_ids = (
            select(func.max(InterventionScore.id).label("max_id"))
            .group_by(InterventionScore.post_id)
            .subquery()
        )
        stmt = (
            select(InterventionScore.priority_label, func.count(InterventionScore.id))
            .where(InterventionScore.id.in_(select(latest_ids.c.max_id)))
            .group_by(InterventionScore.priority_label)
        )
        return {label: int(cnt) for label, cnt in db.execute(stmt).all()}

    @staticmethod
    def avg_risk(db: Session) -> float | None:
        """Mean misinfo risk across the LATEST score of each monitored post."""
        latest_ids = (
            select(func.max(MisinformationScore.id).label("max_id"))
            .group_by(MisinformationScore.post_id)
            .subquery()
        )
        stmt = select(func.avg(MisinformationScore.risk_score)).where(
            MisinformationScore.id.in_(select(latest_ids.c.max_id)))
        val = db.execute(stmt).scalar_one()
        return float(val) if val is not None else None


# ----------------------------------------------------------------- data source status
class DataSourceStatusRepository:
    @staticmethod
    def upsert(
        db: Session,
        platform: str,
        *,
        status: str | None = None,
        last_successful_fetch: datetime | None = None,
        last_error: str | None = None,
        request_count: int | None = None,
        error_count: int | None = None,
        rate_limit_status: str | None = None,
    ) -> DataSourceStatus:
        row = db.execute(select(DataSourceStatus).where(DataSourceStatus.platform == platform)).scalar_one_or_none()
        if row is None:
            row = DataSourceStatus(platform=platform)
            db.add(row)
        if status is not None:
            row.status = status
        if last_successful_fetch is not None:
            row.last_successful_fetch = last_successful_fetch
        if last_error is not None:
            row.last_error = last_error[:1000]
        if request_count is not None:
            row.request_count = request_count
        if error_count is not None:
            row.error_count = error_count
        if rate_limit_status is not None:
            row.rate_limit_status = rate_limit_status
        db.flush()
        return row

    @staticmethod
    def all(db: Session) -> Sequence[DataSourceStatus]:
        return db.execute(select(DataSourceStatus).order_by(DataSourceStatus.platform)).scalars().all()


def serialize_top_factors(raw: str | None) -> list[str]:
    """top_factors is stored as a JSON array string; parse defensively."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


# Governance domain repositories (users, tokens, alerts, cases, audit, models).
from .governance import (  # noqa: E402,F401
    AlertRepository,
    AuditRepository,
    CaseRepository,
    ModelVersionRepository,
    SessionTokenRepository,
    UserRepository,
)

__all__ = [
    "PostRepository", "MetricSnapshotRepository", "PropagationRepository",
    "FeatureSnapshotRepository", "PredictionRepository", "MisinformationRepository",
    "InterventionRepository", "DataSourceStatusRepository", "serialize_top_factors",
    # governance
    "UserRepository", "SessionTokenRepository", "AlertRepository",
    "CaseRepository", "AuditRepository", "ModelVersionRepository",
]

"""Dashboard aggregation endpoints (spec #26, #30-33)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.repositories import (
    InterventionRepository,
    MetricSnapshotRepository,
    MisinformationRepository,
    PostRepository,
    PredictionRepository,
    serialize_top_factors,
)
from app.ml.inference import ModelManager
from app.schemas import (
    DashboardSummary,
    HighPriorityPost,
    HighPriorityResponse,
    TrendingPost,
    TrendingResponse,
)
from app.services.ingestion_service import scheduler

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _latest_metrics_map(db: Session, post_ids: list[int]) -> dict[int, object]:
    return {pid: MetricSnapshotRepository.latest(db, pid) for pid in post_ids}


@router.get("/summary", response_model=DashboardSummary, summary="KPI summary",
            description="All dashboard KPI counters computed from live data — nothing hardcoded.")
def summary(db: Session = Depends(get_db)) -> DashboardSummary:
    # 20s TTL cache (spec #18); invalidated by the ingestion loop via cache.bump().
    from app.core.cache import cache
    cached = cache.get("dashboard::summary")
    if cached is not None:
        return cached
    total_posts = PostRepository.count_all(db)
    label_counts = InterventionRepository.label_counts(db)
    critical = label_counts.get("CRITICAL", 0)
    high = label_counts.get("HIGH", 0)
    predicted_60m = PredictionRepository.sum_predicted_additional(db, 60)
    avg_risk = MisinformationRepository.avg_risk(db)

    # platform counts
    posts, _ = PostRepository.list_posts(db, limit=1000)
    platform_counts: dict[str, int] = {}
    for p in posts:
        platform_counts[p.platform] = platform_counts.get(p.platform, 0) + 1

    dto = DashboardSummary(
        posts_monitored=total_posts,
        critical_alerts=critical,
        high_risk_posts=high + critical,  # HIGH + CRITICAL
        predicted_shares_60m=round(predicted_60m, 1),
        average_risk=round(avg_risk, 4) if avg_risk is not None else None,
        platform_counts=platform_counts,
        label_counts=label_counts,
        ingestion=scheduler.status(),
        models=ModelManager.instance().status(),
        last_update=datetime.now(timezone.utc),
    )
    cache.set("dashboard::summary", dto)
    return dto


@router.get("/high-priority", response_model=HighPriorityResponse, summary="High-priority queue",
            description="Latest intervention score per post, filtered by label/platform, sorted by priority.")
def high_priority(
    label: Optional[str] = Query(None, description="LOW|MEDIUM|HIGH|CRITICAL — or omit for all"),
    min_priority: float = Query(0.0, ge=0, le=100),
    platform: Optional[str] = None,
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> HighPriorityResponse:
    rows = InterventionRepository.high_priority(
        db, min_priority=min_priority, platform=platform, label=label, limit=limit)
    out: list[HighPriorityPost] = []
    for score, post in rows:
        latest = MetricSnapshotRepository.latest(db, post.id)
        f60 = PredictionRepository.latest_for_post(db, post.id, horizon_minutes=60)
        misinfo = MisinformationRepository.latest_for_post(db, post.id)
        out.append(HighPriorityPost(
            post_id=post.id,
            platform=post.platform,
            external_post_id=post.external_post_id,
            content=post.content,
            is_demo=post.is_demo,
            current_shares=float(latest.shares) if latest and latest.shares is not None else None,
            share_velocity=None,  # velocity comes from features endpoint / detail view
            share_acceleration=None,
            predicted_additional_shares=f60.predicted_additional_shares if f60 else None,
            misinformation_risk=misinfo.risk_score if misinfo else None,
            intervention_priority=score.intervention_priority,
            priority_label=score.priority_label,
            top_factors=serialize_top_factors(score.top_factors),
            timestamp=score.timestamp,
        ))
    return HighPriorityResponse(total=len(out), posts=out)


@router.get("/trending", response_model=TrendingResponse, summary="Trending posts by share growth",
            description="Posts ranked by recent share velocity growth — the 'spreading fastest' view.")
def trending(
    platform: Optional[str] = None,
    limit: int = Query(15, ge=1, le=50),
    db: Session = Depends(get_db),
) -> TrendingResponse:
    from app.services.feature_service import FeatureService

    posts, _ = PostRepository.list_posts(db, platform=platform, limit=300)
    scored: list[tuple[float, TrendingPost]] = []
    for post in posts:
        try:
            features = FeatureService.compute_latest(db, post)
        except Exception:
            continue
        velocity = features.get("share_velocity") or 0.0
        latest = MetricSnapshotRepository.latest(db, post.id)
        f60 = PredictionRepository.latest_for_post(db, post.id, horizon_minutes=60)
        score_row = InterventionRepository.latest_for_post(db, post.id)
        misinfo = MisinformationRepository.latest_for_post(db, post.id)
        scored.append((velocity, TrendingPost(
            post_id=post.id,
            platform=post.platform,
            external_post_id=post.external_post_id,
            content=post.content[:280],
            is_demo=post.is_demo,
            current_shares=float(latest.shares) if latest and latest.shares is not None else None,
            share_velocity=round(velocity, 3) if features.get("share_velocity") is not None else None,
            share_acceleration=features.get("share_acceleration"),
            intervention_priority=score_row.intervention_priority if score_row else None,
            priority_label=score_row.priority_label if score_row else None,
            misinformation_risk=misinfo.risk_score if misinfo else None,
            predicted_additional_shares_60m=f60.predicted_additional_shares if f60 else None,
            timestamp=latest.timestamp if latest else datetime.now(timezone.utc),
        )))
    scored.sort(key=lambda t: t[0], reverse=True)
    return TrendingResponse(total=len(scored), posts=[t for _, t in scored[:limit]])

"""Post endpoints: list, detail, metric history, feature snapshots (spec #26)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.repositories import (
    FeatureSnapshotRepository,
    MetricSnapshotRepository,
    PostRepository,
    PropagationRepository,
)
from app.schemas import (
    FeatureSnapshotOut,
    MetricSnapshotOut,
    PaginatedMetrics,
    PostListResponse,
    PostOut,
)

router = APIRouter(prefix="/posts", tags=["posts"])


def _to_post_out(db: Session, post) -> PostOut:
    latest = MetricSnapshotRepository.latest(db, post.id)
    return PostOut(
        id=post.id,
        platform=post.platform,
        external_post_id=post.external_post_id,
        author_id=post.author_id,
        author_display_name=post.author_display_name,
        content=post.content,
        language=post.language,
        url=post.url,
        posted_at=post.posted_at,
        is_demo=post.is_demo,
        latest_metrics=MetricSnapshotOut(
            timestamp=latest.timestamp,
            likes=latest.likes,
            comments=latest.comments,
            shares=latest.shares,
            views=latest.views,
            followers=latest.followers,
            unique_sharers=latest.unique_sharers,
        ) if latest else None,
    )


@router.get("", response_model=PostListResponse, summary="List monitored posts",
            description="Paginated, filterable list of monitored posts with their latest metric snapshot.")
def list_posts(
    platform: Optional[str] = Query(None, description="Filter by platform (demo|x|reddit|instagram|facebook|linkedin)"),
    search: Optional[str] = Query(None, description="Search in content/author"),
    posted_after: Optional[datetime] = Query(None),
    posted_before: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PostListResponse:
    posts, total = PostRepository.list_posts(
        db, platform=platform, search=search, posted_after=posted_after,
        posted_before=posted_before, limit=limit, offset=offset)
    return PostListResponse(
        total=total, limit=limit, offset=offset,
        posts=[_to_post_out(db, p) for p in posts],
    )


@router.get("/{post_id}", response_model=PostOut, summary="Post detail",
            responses={404: {"description": "Post not found"}})
def get_post(post_id: int, db: Session = Depends(get_db)) -> PostOut:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    return _to_post_out(db, post)


@router.get("/{post_id}/metrics", response_model=PaginatedMetrics, summary="Metric snapshot history",
            description="Time-series of raw metric snapshots (the chart data source). Supports time-range filtering.")
def post_metrics(
    post_id: int,
    window_minutes: Optional[int] = Query(None, ge=1, le=1440, description="Only snapshots from the last N minutes (30/60/120)"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PaginatedMetrics:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes) if window_minutes else None
    history = MetricSnapshotRepository.history(db, post_id, since=since)
    total = len(history)
    page = history[offset:offset + limit]
    return PaginatedMetrics(
        post_id=post_id, total=total, limit=limit, offset=offset,
        snapshots=[MetricSnapshotOut(
            timestamp=s.timestamp, likes=s.likes, comments=s.comments,
            shares=s.shares, views=s.views, followers=s.followers,
            unique_sharers=s.unique_sharers) for s in page],
    )


@router.get("/{post_id}/propagation", summary="Propagation cascade edges",
            description="Reshare network events for the propagation network view. Empty when a "
                        "platform does not expose reshare graphs (documented API limitation).")
def post_propagation(post_id: int, limit: int = Query(300, ge=1, le=1000),
                           db: Session = Depends(get_db)) -> dict:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    events = PropagationRepository.for_post(db, post_id, limit=limit)
    return {
        "post_id": post_id,
        "platform": post.platform,
        "is_demo": post.is_demo,
        "posted_at": post.posted_at,
        "total": len(events),
        "events": [
            {
                "source_user_id": e.source_user_id,
                "target_user_id": e.target_user_id,
                "event_type": e.event_type,
                "timestamp": e.timestamp,
                "time_since_original_post": e.time_since_original_post,
                "depth": e.depth,
            } for e in events
        ],
    }


@router.get("/{post_id}/features", response_model=list[FeatureSnapshotOut], summary="Engineered feature history")
def post_features(
    post_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[FeatureSnapshotOut]:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    rows = FeatureSnapshotRepository.history(db, post_id)[-limit:]
    out = []
    for r in rows:
        data = {c.name: getattr(r, c.name) for c in type(r).__table__.columns
                if c.name not in {"id", "post_id"}}
        out.append(FeatureSnapshotOut(**data))
    return out

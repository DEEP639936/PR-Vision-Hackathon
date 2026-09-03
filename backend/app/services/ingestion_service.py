"""Data ingestion service (spec #11).

Responsibilities:
    - ingest_new_post(): full path for a NEW post (normalizer → DB → backfill)
    - poll_platform(): metric refresh for all monitored posts of one platform
    - IngestionScheduler: asyncio background loop per platform with
      configurable interval, retry with exponential backoff, per-platform
      failure isolation (one broken connector never affects others), and
      DataSourceStatus bookkeeping.

Every payload — demo or real — flows through the SAME normalizer/database
path. Connector failures are logged and surfaced via /api/health and
/api/platforms, never raised into the request path.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.connectors import get_connector
from app.connectors.base import ConnectorError, RateLimitedError, SocialPlatformConnector
from app.core.config import settings
from app.core.cache import cache
from app.core.logging import get_logger
from app.db.models import Post
from app.db.repositories import (
    DataSourceStatusRepository,
    MetricSnapshotRepository,
    PostRepository,
    PredictionRepository,
    PropagationRepository,
)
from app.services.prediction_service import PredictionService

logger = get_logger("prvision.ingestion")

MAX_BACKOFF_SECONDS = 120

# SQLite allows exactly ONE writer. With the big-5 harvesters plus the
# mastodon/hackernews loops all running against the same demo DB, concurrent
# cycle writes would collide ("database is locked"). The scheduler is fully
# async, so a single asyncio lock serialises the write phase of each platform
# cycle without blocking the event loop (HTTP waits happen between lock uses
# of OTHER loops only while their own session block holds it).
_INGEST_DB_LOCK = asyncio.Lock()
_SCORE_LOCK = threading.Lock()  # never run two score sweeps concurrently


class IngestionService:
    """Stateless helpers used by both the scheduler and the API routes."""

    @staticmethod
    async def ingest_new_post(
        db: Session,
        connector: SocialPlatformConnector,
        post_payload: Any,
        *,
        is_demo: bool | None = None,
    ) -> Post:
        """Normalize → upsert post → store initial snapshot + propagation."""
        normalized = post_payload if not isinstance(post_payload, dict) else post_payload
        platform = getattr(normalized, "platform", None) or normalized["platform"]
        external_id = getattr(normalized, "post_id", None) or normalized["post_id"]

        existing = PostRepository.get_by_external_id(db, platform, external_id)
        if existing:
            return existing

        post = PostRepository.create(
            db,
            platform=platform,
            external_post_id=external_id,
            author_id=getattr(normalized, "author_id", None) or "unknown",
            author_display_name=getattr(normalized, "author_display_name", None),
            content=getattr(normalized, "content", None) or "",
            language=getattr(normalized, "language", None),
            url=getattr(normalized, "url", None),
            posted_at=getattr(normalized, "posted_at", None) or datetime.now(timezone.utc),
            is_demo=is_demo if is_demo is not None else bool(getattr(normalized, "is_demo", False)),
        )
        MetricSnapshotRepository.add(
            db,
            post_id=post.id,
            timestamp=post.posted_at,
            likes=getattr(normalized, "likes", None),
            comments=getattr(normalized, "comments", None),
            shares=getattr(normalized, "shares", None),
            views=getattr(normalized, "views", None),
            followers=getattr(normalized, "followers", None),
            unique_sharers=getattr(normalized, "unique_sharers", None),
        )
        db.flush()
        return post

    @staticmethod
    async def store_propagation(db: Session, post: Post, events: list) -> int:
        if not events:
            return 0
        payloads = []
        for e in events:
            source = getattr(e, "source_user_id", None)
            if source is None and isinstance(e, dict):
                source = e.get("source_user_id")
            payloads.append({
                "source_user_id": source,
                "target_user_id": getattr(e, "target_user_id", None),
                "event_type": getattr(e, "event_type", "share"),
                "timestamp": getattr(e, "timestamp", None) or datetime.now(timezone.utc),
                "time_since_original_post": getattr(e, "time_since_original_post", None),
                "depth": getattr(e, "depth", None),
            })
        return PropagationRepository.bulk_add(db, post.id, payloads)

    @staticmethod
    async def poll_platform(db: Session, platform: str) -> dict[str, Any]:
        """One metric-refresh cycle for every monitored post on a platform.

        Connector calls are async I/O (event-loop friendly); per-post DB work
        is milliseconds. The CPU-heavy re-scoring is dispatched to a worker
        thread in refresh_scores so the loop never blocks (spec #40/41).
        """
        connector = get_connector(platform)
        posts, _ = PostRepository.list_posts(db, platform=platform, limit=500)
        updated, errors = 0, 0

        # --- new-post discovery (keyless harvesters + public-API platforms) --
        # Runs on the connector's own throttle so the shared scheduler interval
        # never hammers search/API endpoints. Ingestion failures are isolated:
        # a bad result only skips that one payload.
        if getattr(connector, "supports_discovery", False):
            interval = getattr(settings, "HARVEST_DISCOVERY_INTERVAL_SECONDS", 900)
            now_mono = time.monotonic()
            if now_mono - getattr(connector, "_last_discovery", 0.0) >= interval:
                connector._last_discovery = now_mono
                try:
                    discovered = await connector.fetch_posts(
                        limit=settings.HARVEST_POSTS_PER_DISCOVERY)
                    for payload in discovered:
                        try:
                            existing = PostRepository.get_by_external_id(
                                db, platform, payload.post_id)
                            if existing:
                                continue
                            created = await IngestionService.ingest_new_post(
                                db, connector, payload)
                            if created is not None:
                                updated += 1
                                posts.append(created)
                            # Short SQLite write transactions: commit per post so
                            # the writer lock is held for milliseconds, not for
                            # the whole discovery burst (verify jobs + API writes
                            # then never wait on us longer than one post).
                            db.commit()
                        except Exception:
                            errors += 1
                            db.rollback()
                            logger.exception("Discovery ingest failed for %s/%s",
                                             platform, getattr(payload, "post_id", "?"))
                    if discovered:
                        logger.info("Discovery %s: %d result(s), %d new", platform,
                                    len(discovered), updated)
                except ConnectorError as exc:
                    errors += 1
                    logger.warning("Discovery failed for %s: %s", platform, exc)
                    DataSourceStatusRepository.upsert(
                        db, platform, status="degraded", last_error=str(exc)[:500])
                except Exception:
                    errors += 1
                    logger.exception("Unexpected discovery error for %s", platform)

        def _as_utc(dt: datetime) -> datetime:
            # DB may hand back naive datetimes on SQLite; connectors emit
            # UTC-aware ones. Normalise before any comparison (spec #16).
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

        # Metrics refresh respects the connector's politeness window (public
        # APIs and instances get throttled; harvesters skip instantly).
        poll_metrics = (time.monotonic() - getattr(connector, "_last_metric_poll", 0.0)
                        >= getattr(connector, "min_poll_seconds", 0.0))
        for post in posts:
            try:
                if not poll_metrics:
                    continue
                latest = MetricSnapshotRepository.latest(db, post.id)
                latest_ts = _as_utc(latest.timestamp) if latest else None
                metrics = await connector.fetch_post_metrics(
                    post.external_post_id,
                    since=latest_ts,
                    post_posted_at=post.posted_at,
                )
                for snap in metrics:
                    if latest_ts and _as_utc(snap.timestamp) <= latest_ts:
                        continue
                    MetricSnapshotRepository.add(
                        db, post_id=post.id, timestamp=snap.timestamp,
                        likes=snap.likes, comments=snap.comments, shares=snap.shares,
                        views=snap.views, followers=snap.followers,
                        unique_sharers=snap.unique_sharers,
                    )
                    updated += 1
                events = await connector.fetch_propagation_data(
                    post.external_post_id, since=latest_ts)
                await IngestionService.store_propagation(db, post, events)
                # Commit per post — keeps each write txn short (see discovery note).
                db.commit()
            except ConnectorError as exc:
                errors += 1
                if getattr(exc, "kind", "") == "rate_limited":
                    retry_after = getattr(exc, "retry_after", None)
                    DataSourceStatusRepository.upsert(
                        db, platform,
                        status="rate_limited",
                        rate_limit_status=(f"429 rate limited; retry after {int(retry_after)}s"
                                           if retry_after else "429 rate limited"),
                        last_error=str(exc)[:500])
                    logger.warning("Poll rate-limited for post %s on %s", post.id, platform)
                else:
                    logger.warning("Poll failed for post %s on %s: %s", post.id, platform, exc)
            except Exception:
                errors += 1
                logger.exception("Unexpected poll error for post %s on %s", post.id, platform)
        if poll_metrics:
            connector._last_metric_poll = time.monotonic()

        DataSourceStatusRepository.upsert(
            db, platform,
            status="healthy" if errors == 0 else ("degraded" if updated or errors < max(1, len(posts)) else "error"),
            last_successful_fetch=datetime.now(timezone.utc) if (updated or not errors) else None,
            last_error=f"{errors} post(s) failed" if errors else None,
        )
        return {"platform": platform, "posts_polled": len(posts), "snapshots_added": updated, "errors": errors}


class IngestionScheduler:
    """Async background scheduler — one loop per platform, isolated failures."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stagger: dict[str, float] = {}
        self._running = False
        self._interval = settings.INGESTION_INTERVAL_SECONDS
        self._consecutive_failures: dict[str, int] = {}
        self.last_cycle_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "platforms": list(self._tasks.keys()),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "consecutive_failures": dict(self._consecutive_failures),
        }

    async def start(self, platforms: Optional[list[str]] = None, interval: Optional[int] = None) -> dict:
        if self._running:
            return {"status": "already_running", **self.status()}
        self._interval = max(10, interval or self._interval)
        platforms = platforms or ["demo"]
        self._running = True
        # Stagger loop first-cycles across the interval window so the SQLite
        # write transactions of different platforms rarely coincide with each
        # other (or with API-path writes) — WAL serialises writers, and a
        # writer waiting on the busy timeout inside the event-loop thread
        # would briefly freeze the whole service.
        stagger_step = max(1.0, (self._interval * 0.7) / max(1, len(platforms)))
        for index, platform in enumerate(platforms):
            self._stagger[platform] = round(index * stagger_step, 1)
            self._tasks[platform] = asyncio.create_task(
                self._loop(platform), name=f"ingest-{platform}")
        logger.info("Ingestion scheduler started: platforms=%s interval=%ss stagger=%s",
                    platforms, self._interval,
                    {p: self._stagger[p] for p in platforms})
        return {"status": "started", **self.status()}

    async def stop(self) -> dict:
        self._running = False
        for platform, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("Ingestion scheduler stopped")
        return self.status()

    async def _loop(self, platform: str) -> None:
        backoff = 1
        first_cycle = True
        while self._running:
            try:
                if first_cycle and self._stagger.get(platform):
                    await asyncio.sleep(self._stagger[platform])
                first_cycle = False
                from app.db.database import session_scope
                # IMPORTANT: the ingestion transaction must COMMIT before any
                # re-scoring runs. refresh_scores uses its own DB session; if
                # awaited inside this session_scope block it would contend
                # with this session's uncommitted writes (SQLite allows one
                # writer) and busy-timeout after 30s. Commit first, then score.
                # The lock additionally serialises concurrent platform loops
                # (writes + the score sweep) so their write transactions never
                # fight for SQLite's writer from different threads — a writer
                # blocked on the busy timeout inside the EVENT LOOP thread
                # would freeze the whole service, so writers must stay mutual.
                async with _INGEST_DB_LOCK:
                    with session_scope() as db:
                        result = await IngestionService.poll_platform(db, platform)
                        snapshots_added = result["snapshots_added"]
                    if snapshots_added:
                        await self.refresh_scores()
                self._consecutive_failures[platform] = 0
                backoff = 1
                self.last_cycle_at = datetime.now(timezone.utc)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures = self._consecutive_failures.get(platform, 0) + 1
                self._consecutive_failures[platform] = failures
                delay = min(MAX_BACKOFF_SECONDS, self._interval * (2 ** min(failures, 5)))
                logger.error("Ingestion loop %s failed (%s consecutive): %s — retrying in %ss",
                             platform, failures, exc, delay)
                try:
                    from app.db.database import session_scope
                    async with _INGEST_DB_LOCK:
                        with session_scope() as db:
                            DataSourceStatusRepository.upsert(
                                db, platform, status="error",
                                last_error=str(exc)[:500],
                                error_count=failures)
                except Exception:
                    logger.exception("Could not record error status for %s", platform)
                await asyncio.sleep(delay)

    async def refresh_scores(self, limit: int = 100) -> int:
        """Re-score monitored posts (worker thread — keeps the loop free)."""
        return await asyncio.to_thread(IngestionScheduler._refresh_scores_sync, limit)

    @staticmethod
    def _refresh_scores_sync(limit: int = 100) -> int:
        from app.db.database import session_scope
        scored = 0
        with _SCORE_LOCK:
            # Snapshot the post ids first, then score each post in its OWN
            # short write transaction. One long transaction for the whole
            # sweep would hold SQLite's single writer lock for seconds and
            # freeze every API-path writer (their busy-wait runs on the event
            # loop thread). Per-post transactions keep each write ~ms.
            with session_scope() as db:
                posts, _ = PostRepository.list_posts(db, limit=limit, offset=0)
                post_ids = [p.id for p in posts]
            for pid in post_ids:
                try:
                    with session_scope() as db:
                        post = db.get(Post, pid)
                        if post is None:
                            continue
                        PredictionService.score_post(db, post)
                        scored += 1
                except Exception:
                    logger.exception("Score refresh failed for post %s", pid)
            # Alert engine reads the committed scores in its own transaction.
            try:
                from app.services.alert_engine import evaluate_early_warning_sync
                with session_scope() as db:
                    evaluate_early_warning_sync(db, limit=limit)
            except Exception:
                logger.exception("Alert evaluation failed (non-fatal)")
        cache.bump("dashboard")
        cache.bump("platforms")
        return scored


scheduler = IngestionScheduler()

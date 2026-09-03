"""Demo data + ingestion control endpoints (spec #10, #11, #26)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.connectors.demo import ARCHETYPES
from app.db.database import get_db
from app.schemas import (
    DemoGenerateRequest,
    DemoGenerateResponse,
    IngestionStartRequest,
    IngestionStatusResponse,
)
from app.services.demo_service import ARCHETYPE_DESCRIPTIONS, DemoService
from app.services.ingestion_service import IngestionService, scheduler

router = APIRouter(tags=["demo & ingestion"])


# ---------------------------------------------------------------------- demo
@router.post("/demo/generate", response_model=DemoGenerateResponse,
             summary="Generate realistic demo posts (full pipeline)",
             description="Creates demo posts with realistic temporal propagation history. "
                         "Demo data flows through the SAME normalizer → MySQL → feature engineering "
                         "→ ML → intervention score path as real platform data. Posts are badged is_demo=true.")
async def generate_demo(request: DemoGenerateRequest, db: Session = Depends(get_db)) -> DemoGenerateResponse:
    invalid = [a for a in (request.archetypes or []) if a not in ARCHETYPES]
    if invalid:
        raise HTTPException(422, f"Unknown archetypes: {invalid}. Valid: {list(ARCHETYPES)}")
    created = await DemoService.generate_posts(
        db, num_posts=request.num_posts, archetypes=request.archetypes, score=request.score)
    return DemoGenerateResponse(created=len(created), posts=created)


@router.get("/demo/archetypes", summary="Describe demo archetypes",
            description="The five demo behaviours, including the key 'not every viral post is misinformation' pair.")
async def archetypes() -> dict:
    return {"archetypes": [{"name": a, "description": ARCHETYPE_DESCRIPTIONS[a]} for a in ARCHETYPES]}


# ----------------------------------------------------------------- ingestion
@router.post("/ingestion/start", response_model=IngestionStatusResponse,
             summary="Start background ingestion polling",
             description="Starts per-platform async polling loops (default: demo platform). "
                         "Each platform loop is isolated — one failing connector never stops the others.")
async def start_ingestion(request: IngestionStartRequest) -> IngestionStatusResponse:
    result = await scheduler.start(platforms=request.platforms, interval=request.interval_seconds)
    return IngestionStatusResponse(**result, detail="background polling active")


@router.post("/ingestion/stop", response_model=IngestionStatusResponse,
             summary="Stop background ingestion polling")
async def stop_ingestion() -> IngestionStatusResponse:
    result = await scheduler.stop()
    return IngestionStatusResponse(**result, detail="all polling loops cancelled")


@router.get("/ingestion/status", response_model=IngestionStatusResponse,
            summary="Ingestion scheduler status")
async def ingestion_status() -> IngestionStatusResponse:
    return IngestionStatusResponse(**scheduler.status())


@router.post("/ingestion/poll-once", summary="Run a single polling cycle now",
             description="Manual metric refresh for one platform (useful for demos and tests).")
async def poll_once(platform: str = Query("demo"), db: Session = Depends(get_db)) -> dict:
    try:
        result = await IngestionService.poll_platform(db, platform)
    except Exception as exc:
        raise HTTPException(502, f"Poll failed for platform '{platform}': {exc}")
    return result

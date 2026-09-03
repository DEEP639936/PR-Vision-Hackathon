"""Health endpoint — reflects REAL system state (spec #45)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import check_database_connection, get_db
from app.ml.inference import ModelManager
from app.schemas import HealthResponse
from app.services.ingestion_service import scheduler

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="System health check",
            description="Returns the ACTUAL state of database, ML models and ingestion scheduler. Never reports fake healthy values.")
def health(db: Session = Depends(get_db)) -> HealthResponse:
    db_ok = check_database_connection()
    manager = ModelManager.instance()
    model_ready = manager.is_forecast_ready()
    ingestion_status = "running" if scheduler.running else "stopped"

    if db_ok and model_ready:
        status = "healthy"
    elif db_ok:
        status = "degraded"  # DB fine but models not trained yet (cold start)
    else:
        status = "unhealthy"

    return HealthResponse(
        status=status,
        database="connected" if db_ok else "disconnected",
        forecast_model="loaded" if model_ready else "not_loaded",
        ingestion=ingestion_status,
        platforms_active=sorted(scheduler.status()["platforms"]) if scheduler.running else [],
        version=settings.APP_VERSION,
        app_env=settings.APP_ENV,
        time=datetime.now(timezone.utc),
    )

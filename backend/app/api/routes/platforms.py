"""Platform connector status endpoints (spec #9, #26)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.connectors import (
    HARVEST_PLATFORMS,
    SUPPORTED_PLATFORMS,
    get_connector,
)
from app.connectors.base import OFFICIAL_API_KEY_HINTS
from app.core.config import settings
from app.db.database import get_db
from app.db.repositories import DataSourceStatusRepository
from app.schemas import PlatformListResponse, PlatformStatus

router = APIRouter(prefix="/platforms", tags=["platforms"])


@router.get("", response_model=PlatformListResponse,
            summary="List supported platforms with REAL connector status",
            description="Shows which platform connectors are configured (credentials present), "
                        "their live health, and persistent fetch statistics. Platforms without "
                        "credentials report not_configured — the system never pretends to be connected.")
async def list_platforms(db: Session = Depends(get_db)) -> PlatformListResponse:
    statuses: list[PlatformStatus] = []
    persisted = {row.platform: row for row in DataSourceStatusRepository.all(db)}

    for platform in SUPPORTED_PLATFORMS:
        connector = get_connector(platform)
        configured = settings.is_platform_configured(platform)
        row = persisted.get(platform)
        health = None
        detail = None
        # Health probes only for configured (or demo) connectors to avoid pointless API calls.
        if configured:
            try:
                status_obj = await connector.health_check()
                health = status_obj.healthy
                detail = status_obj.detail
            except Exception as exc:  # never fail the listing
                health = False
                detail = str(exc)[:200]

        # Map to spec #28 canonical states — never pretend a connector works.
        internal = row.status if row else ("healthy" if platform == "demo" else "not_configured")
        state = _canonical_state(platform, internal, configured, health, row)
        if state == "HARVEST":
            detail = (f"REAL public posts ingested via the keyless web-search "
                      f"harvester; official API credentials not configured — "
                      f"set {OFFICIAL_API_KEY_HINTS.get(platform, 'API_KEY')} in "
                      f".env and restart to stream the official API")
        statuses.append(PlatformStatus(
            platform=platform,
            configured=configured,
            status=internal,
            state=state,
            healthy=health if configured else None,
            detail=detail,
            last_successful_fetch=row.last_successful_fetch if row else None,
            last_error=row.last_error if row else None,
            request_count=row.request_count if row else 0,
            error_count=row.error_count if row else 0,
            rate_limit_status=row.rate_limit_status if row else None,
        ))
    return PlatformListResponse(platforms=statuses)


def _canonical_state(platform: str, internal: str, configured: bool,
                     health: Optional[bool], row) -> str:
    """Map internal statuses to the spec #28 connector states (+HARVEST).

    HARVEST: the big-5 platforms without official credentials still ingest
    REAL public posts through the sidecar web-search harvester. The state is
    only reported when that pipeline actually succeeded recently — otherwise
    the platform stays honestly DISABLED.
    """
    if (not configured
            and platform in HARVEST_PLATFORMS
            and row is not None
            and row.status == "healthy"
            and row.last_successful_fetch is not None):
        return "HARVEST"
    if not configured:
        return "DISABLED"
    if (row is not None and row.rate_limit_status) or internal == "rate_limited":
        return "RATE_LIMITED"
    if internal in ("auth_required", "not_configured"):
        return "AUTH_REQUIRED"
    if internal == "degraded" or health is False:
        return "DEGRADED"
    if internal in ("error", "unavailable") or health is False:
        return "UNAVAILABLE"
    if health is True or internal in ("healthy", "configured"):
        return "CONNECTED"
    return "UNAVAILABLE"

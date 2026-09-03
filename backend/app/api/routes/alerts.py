"""Alerts API (spec #13).

Endpoints:
    GET  /api/alerts            list alerts (filter: severity, acknowledged)
    GET  /api/alerts/summary    unacknowledged counts by severity
    POST /api/alerts/{id}/ack   acknowledge an alert (auth; audit-logged)
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User
from app.db.repositories import AlertRepository, AuditRepository
from app.core.security import require_user

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AckRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


def _alert_dto(alert) -> dict[str, Any]:
    return {
        "alert_id": alert.id,
        "severity": alert.severity,
        "kind": alert.kind,
        "title": alert.title,
        "message": alert.message,
        "metrics": alert.metrics or {},
        "post_id": alert.post_id,
        "verification_job_id": alert.verification_job_id,
        "acknowledged": alert.acknowledged_at is not None,
        "acknowledged_by": alert.acknowledged_by,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    }


@router.get("", summary="List early-warning alerts")
def list_alerts(
    severity: Optional[Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]] = None,
    acknowledged: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows, total = AlertRepository.list_alerts(
        db, severity=severity, acknowledged=acknowledged, limit=limit, offset=offset)
    return {"total": total, "alerts": [_alert_dto(a) for a in rows]}


@router.get("/summary", summary="Unacknowledged alert counts by severity")
def alert_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    counts = AlertRepository.counts_by_severity(db)
    return {"unacknowledged": counts,
            "total_unacknowledged": sum(counts.values())}


@router.post("/{alert_id}/ack", summary="Acknowledge an alert")
def acknowledge_alert(
    alert_id: int, body: AckRequest, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    alert = AlertRepository.acknowledge(db, alert_id, user.display_name or user.email)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    AuditRepository.record(
        db, actor=user.email, action="alert.ack", target_type="alert", target_id=alert.id,
        detail=body.note, ip=request.client.host if request.client else None)
    return _alert_dto(alert)

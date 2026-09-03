"""Investigation cases API (spec #14).

Endpoints:
    POST   /api/cases                save a completed verification as a case (auth)
    GET    /api/cases                list cases (auth)
    GET    /api/cases/{id}           case detail incl. notes + analysis snapshot (auth)
    PATCH  /api/cases/{id}           update status / title / summary (owner or admin)
    DELETE /api/cases/{id}           delete case (owner or admin)
    POST   /api/cases/{id}/notes     add investigator note (auth)
    GET    /api/cases/{id}/export    delegate to the report exporter (JSON)

Cases snapshot the analysis verdict/priority at save time so the case list
stays meaningful even as the underlying job is re-analysed. All mutations
are audit-logged.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import require_user
from app.db.database import get_db
from app.db.models import Case, User, VerificationJob
from app.db.repositories import AuditRepository, CaseRepository

logger = get_logger("prvision.api.cases")

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseCreate(BaseModel):
    verification_job_id: int = Field(ge=1)
    title: str = Field(min_length=3, max_length=255)
    summary: Optional[str] = Field(default=None, max_length=4000)
    status: Literal["OPEN", "MONITORING", "ESCALATED", "CLOSED"] = "OPEN"


class CaseUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=3, max_length=255)
    summary: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[Literal["OPEN", "MONITORING", "ESCALATED", "CLOSED"]] = None


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


def _case_dto(db: Session, case: Case, *, include_notes: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "case_id": case.id,
        "verification_job_id": case.verification_job_id,
        "title": case.title,
        "summary": case.summary,
        "status": case.status,
        "priority_snapshot": case.priority_snapshot,
        "severity_label": case.severity_label,
        "verdict_snapshot": case.verdict_snapshot,
        "created_by": case.created_by,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        "note_count": len(CaseRepository.notes_for(db, case.id)),
    }
    if include_notes:
        data["notes"] = [{
            "note_id": n.id, "author": n.author_name, "body": n.body,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        } for n in CaseRepository.notes_for(db, case.id)]
    return data


def _snapshot_from_job(db: Session, job: VerificationJob) -> dict[str, Any]:
    summary = json.loads(job.result_summary) if job.result_summary else {}
    priority = (summary or {}).get("priority") or {}
    overall = (summary or {}).get("overall") or {}
    # verify-path priority dict keys: intervention_priority | label | factors;
    # some historic summaries may only carry a scalar under 'score'.
    score = priority.get("intervention_priority")
    if score is None:
        score = priority.get("score")
    label = priority.get("label")
    return {
        "priority_snapshot": score,
        "severity_label": label,
        "verdict_snapshot": overall.get("verdict"),
    }


def _load_case_or_404(db: Session, case_id: int) -> Case:
    case = CaseRepository.get(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def _require_owner_or_admin(case: Case, user: User) -> None:
    if user.role != "admin" and case.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the case owner or an admin can modify this case")


@router.post("", summary="Save a verification analysis as an investigation case")
def create_case(
    body: CaseCreate, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    job = db.get(VerificationJob, body.verification_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Verification job not found")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Job has not completed — only completed analyses can be saved as cases")

    snapshot = _snapshot_from_job(db, job)
    case = CaseRepository.create(
        db,
        verification_job_id=job.id,
        created_by=user.id,
        title=body.title.strip(),
        summary=(body.summary or "").strip() or None,
        status=body.status,
        **snapshot)
    AuditRepository.record(
        db, actor=user.email, action="case.create", target_type="case", target_id=case.id,
        detail=f"job={job.id} title={case.title[:120]}",
        ip=request.client.host if request.client else None)
    logger.info("Case %d created from job %d by %s", case.id, job.id, user.email)
    return _case_dto(db, case, include_notes=True)


@router.get("", summary="List investigation cases")
def list_cases(
    status: Optional[str] = Query(None, pattern="^(OPEN|MONITORING|ESCALATED|CLOSED)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    rows, total = CaseRepository.list_cases(db, status=status, limit=limit, offset=offset)
    return {"total": total, "cases": [_case_dto(db, c) for c in rows]}


@router.get("/{case_id}", summary="Case detail with investigator notes")
def case_detail(
    case_id: int, db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    case = _load_case_or_404(db, case_id)
    dto = _case_dto(db, case, include_notes=True)
    dto["job_status"] = db.get(VerificationJob, case.verification_job_id).status \
        if db.get(VerificationJob, case.verification_job_id) else None
    return dto


@router.patch("/{case_id}", summary="Update case status / title / summary")
def update_case(
    case_id: int, body: CaseUpdate, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    case = _load_case_or_404(db, case_id)
    _require_owner_or_admin(case, user)
    changed = []
    if body.title is not None:
        case.title = body.title.strip()
        changed.append("title")
    if body.summary is not None:
        case.summary = body.summary.strip() or None
        changed.append("summary")
    if body.status is not None and body.status != case.status:
        case.status = body.status
        changed.append(f"status→{body.status}")
    db.flush()
    AuditRepository.record(
        db, actor=user.email, action="case.update", target_type="case", target_id=case.id,
        detail=", ".join(changed) or "no-op",
        ip=request.client.host if request.client else None)
    return _case_dto(db, case)


@router.delete("/{case_id}", summary="Delete a case")
def delete_case(
    case_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    case = _load_case_or_404(db, case_id)
    _require_owner_or_admin(case, user)
    job_id = case.verification_job_id
    CaseRepository.delete(db, case)
    AuditRepository.record(
        db, actor=user.email, action="case.delete", target_type="case", target_id=case_id,
        detail=f"job={job_id}", ip=request.client.host if request.client else None)
    return {"deleted": True}


@router.post("/{case_id}/notes", summary="Add an investigator note")
def add_note(
    case_id: int, body: NoteCreate, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_user),
) -> dict[str, Any]:
    case = _load_case_or_404(db, case_id)
    note = CaseRepository.add_note(
        db, case_id=case.id, author_id=user.id,
        author_name=user.display_name or user.email, body=body.body.strip())
    AuditRepository.record(
        db, actor=user.email, action="case.note", target_type="case", target_id=case.id,
        ip=request.client.host if request.client else None)
    return {"note_id": note.id, "author": note.author_name, "body": note.body,
            "created_at": note.created_at.isoformat() if note.created_at else None}

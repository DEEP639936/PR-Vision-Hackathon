"""Verification & multimodal intelligence API (spec #3, #4, #7, #26).

Endpoints:
    POST   /api/verify                 submit URL / text / file (multipart)
    GET    /api/verify/jobs            recent verification queue
    GET    /api/verify/{id}            job status + progress
    GET    /api/verify/{id}/report     full evidence-first investigation report
    DELETE /api/verify/{id}            cancel a running job
    GET    /api/evidence/providers     live provider health (never simulated)
    GET    /api/factcheck/search       ad-hoc fact-check lookup for a claim
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.core.config import settings
from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    AnalyzedContent,
    Claim,
    ClaimVerdict,
    EvidenceEdge,
    EvidenceItem,
    FactCheckMatch,
    MediaAnalysis,
    NumericalCheck,
    SourceProfile,
    TimelineEvent,
    VerificationJob,
)
from app.services.verification_service import pipeline

logger = get_logger("prvision.api.verify")
router = APIRouter(tags=["verify"])

_ALLOWED_MIME = {
    "pdf", "docx", "doc", "csv", "tsv", "json", "html", "htm", "txt", "md",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff",
}

# Magic-byte signatures (spec #17: file validation — extension alone is not trust).
# Each extension maps to a list of ALTERNATIVE signatures; each alternative is a
# list of (offset, bytes) conditions that must ALL match (compound signature).
_MAGIC: dict[str, list[list[tuple[int, bytes]]]] = {
    "pdf": [[(0, b"%PDF-")]],
    "png": [[(0, b"\x89PNG\r\n\x1a\n")]],
    "jpg": [[(0, b"\xff\xd8\xff")]],
    "jpeg": [[(0, b"\xff\xd8\xff")]],
    "gif": [[(0, b"GIF87a")], [(0, b"GIF89a")]],
    "webp": [[(0, b"RIFF"), (8, b"WEBP")]],
    "bmp": [[(0, b"BM")]],
    "tiff": [[(0, b"II*\x00")], [(0, b"MM\x00*")]],
    "docx": [[(0, b"PK\x03\x04")]],
    "doc": [[(0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")], [(0, b"PK\x03\x04")]],  # OLE2 or zip
}
_TEXT_EXTS = {"csv", "tsv", "json", "html", "htm", "txt", "md"}


def _content_matches_extension(ext: str, data: bytes) -> bool:
    """Magic-byte / structure validation. Text formats must look like text.

    ``_MAGIC`` maps an extension to ALTERNATIVE signatures; each alternative is
    a list of (offset, bytes) conditions that must ALL match (compound). E.g.
    ".doc" accepts an OLE2 container OR a PK (zip) container.
    """
    if not data:
        return False
    sigs = _MAGIC.get(ext)
    if sigs:
        return any(
            all(data[offset:offset + len(sig)] == sig for offset, sig in alternative)
            for alternative in sigs
        )
    if ext in _TEXT_EXTS:
        sample = data[:8192]
        if b"\x00" in sample:  # binary masquerading as text
            return False
        # a majority of printable/whitespace bytes is a good text indicator
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        return len(sample) == 0 or printable / len(sample) >= 0.85
    return False


def _persist_upload(job_id: int, filename: str, data: bytes) -> None:
    """Best-effort persistence of the upload for auditability (dead setting wired)."""
    try:
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:120] or "upload.bin"
        directory = settings.upload_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"job{job_id}_{safe}").write_bytes(data)
    except Exception:  # persistence must never break analysis
        logger.warning("Could not persist upload for job %s", job_id, exc_info=True)


def _job_dto(job: VerificationJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "status": job.status,
        "input_kind": job.input_kind,
        "input_label": job.input_label,
        "progress": job.progress,
        "stage": job.stage,
        "error": job.error,
        "submitted_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result_summary": json.loads(job.result_summary) if job.result_summary else None,
    }


@router.post("/verify", summary="Submit content for multimodal verification",
             description="Accepts a URL, pasted text, or an uploaded file (PDF/DOCX/CSV/HTML/JSON/image). "
                         "Returns a job id; poll /api/verify/{id} then fetch the report.")
async def submit_verification(
    url: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    submitted_by: Optional[str] = Form(None),
) -> dict[str, Any]:
    url = (url or "").strip() or None
    text = (text or "").strip() or None
    file_bytes: Optional[bytes] = None
    filename: Optional[str] = None

    if file is not None:
        filename = file.filename or "upload"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and ext not in _ALLOWED_MIME:
            raise HTTPException(status_code=415, detail=f"Unsupported file type .{ext}")
        file_bytes = await file.read()
        if len(file_bytes) > settings.VERIFY_UPLOAD_MAX_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File exceeds {settings.VERIFY_UPLOAD_MAX_MB} MB limit")
        if not file_bytes:
            file_bytes = None
        elif ext and not _content_matches_extension(ext, file_bytes):
            # Honest rejection: the bytes do not match the claimed type.
            raise HTTPException(status_code=415,
                                detail=f"File content does not match .{ext} type (magic-byte check failed)")

    if not url and not text and not file_bytes:
        raise HTTPException(status_code=400, detail="Provide a url, text, or file")

    if url and (text or file_bytes):
        # allow URL + supplemental text, but primary content is the URL
        text = None if file_bytes else text

    if url:
        # Immediate, honest rejection of private/invalid URLs (spec #17 SSRF guard)
        from app.verification.ingestion import IngestionError, validate_url
        try:
            url = validate_url(url)
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    result = await pipeline.submit(
        "auto",
        url=url, text=text, file_bytes=file_bytes, filename=filename,
        submitted_by=submitted_by,
    )
    if file_bytes and filename and result.get("job_id"):
        _persist_upload(int(result["job_id"]), filename, file_bytes)
    return result


@router.get("/verify/jobs", summary="Recent verification jobs")
def list_jobs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    with session_scope() as db:
        jobs = (db.query(VerificationJob)
                .order_by(VerificationJob.id.desc())
                .limit(limit).all())
        return {"total": len(jobs), "jobs": [_job_dto(j) for j in jobs]}


@router.get("/verify/{job_id}", summary="Verification job status")
def job_status(job_id: int) -> dict[str, Any]:
    with session_scope() as db:
        job = db.get(VerificationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return _job_dto(job)


@router.delete("/verify/{job_id}", summary="Cancel a queued/running job")
async def cancel_job(job_id: int) -> dict[str, Any]:
    ok = await pipeline.cancel(job_id)
    return {"cancelled": ok}


@router.get("/verify/{job_id}/report", summary="Full evidence-first investigation report")
def job_report(job_id: int) -> dict[str, Any]:
    with session_scope() as db:
        job = db.get(VerificationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in ("completed", "failed", "cancelled"):
            return {"job_id": job_id, "status": job.status, "progress": job.progress,
                    "stage": job.stage, "report_ready": False}

        content = db.query(AnalyzedContent).filter(AnalyzedContent.job_id == job_id).first()
        claims = (db.query(Claim).filter(Claim.job_id == job_id)
                  .order_by(Claim.ordinal.asc()).all())
        verdicts = {v.claim_id: v for v in db.query(ClaimVerdict).join(
            Claim, Claim.id == ClaimVerdict.claim_id).filter(Claim.job_id == job_id).all()}
        evidence = (db.query(EvidenceItem).filter(EvidenceItem.job_id == job_id).all())
        fact_checks = (db.query(FactCheckMatch).filter(FactCheckMatch.job_id == job_id).all())
        media = (db.query(MediaAnalysis).filter(MediaAnalysis.job_id == job_id).all())
        numerics = (db.query(NumericalCheck).filter(NumericalCheck.job_id == job_id).all())
        timeline = (db.query(TimelineEvent).filter(TimelineEvent.job_id == job_id)
                    .order_by(TimelineEvent.occurred_at.asc().nullslast()).all())
        edges = db.query(EvidenceEdge).filter(EvidenceEdge.job_id == job_id).all()
        nodes_note = next((e.note for e in edges if e.source_key == "nodes" and e.note), None)

        summary = json.loads(job.result_summary) if job.result_summary else None

        claims_dto = []
        for c in claims:
            v = verdicts.get(c.id)
            claims_dto.append({
                "ordinal": c.ordinal,
                "text": c.text,
                "claim_type": c.claim_type,
                "checkable": c.checkable,
                "claim_confidence": c.claim_confidence,
                "time_context": c.time_context,
                "entities": json.loads(c.entities) if c.entities else [],
                "numbers": json.loads(c.numbers) if c.numbers else [],
                "extraction_method": c.extraction_method,
                "verdict": {
                    "verdict": v.verdict, "confidence": v.confidence,
                    "confidence_rationale": v.confidence_rationale,
                    "supporting_count": v.supporting_count,
                    "contradicting_count": v.contradicting_count,
                    "neutral_count": v.neutral_count,
                    "primary_source_available": v.primary_source_available,
                    "temporal_flag": v.temporal_flag,
                    "explanation": v.explanation,
                    "fused_signals": json.loads(v.fused_signals) if v.fused_signals else {},
                } if v else None,
            })

        evidence_by_claim: dict[int, list] = {}
        for e in evidence:
            evidence_by_claim.setdefault(e.claim_id or 0, []).append({
                "id": e.id, "provider": e.provider, "url": e.url, "title": e.title,
                "snippet": e.snippet, "publisher": e.publisher, "published_at": e.published_at,
                "stance": e.stance, "stance_confidence": e.stance_confidence,
                "relevance": e.relevance, "quality": e.quality,
                "source_classification": e.source_classification,
                "independence_cluster": e.independence_cluster,
            })
        for dto in claims_dto:
            cid = next((c.id for c in claims if c.ordinal == dto["ordinal"]), 0)
            dto["evidence"] = evidence_by_claim.get(cid, [])
            dto["fact_checks"] = [{
                "provider": f.provider, "claim_text": f.claim_text,
                "textual_rating": f.textual_rating, "publisher": f.publisher,
                "published_at": f.published_at, "url": f.url, "review_snippet": f.review_snippet,
            } for f in fact_checks if f.claim_id == cid]

        graph = {"nodes": json.loads(nodes_note) if nodes_note else [],
                 "edges": [{"source": e.source_key, "edge_type": e.edge_type,
                            "target": e.target_key, "weight": e.weight, "note": e.note,
                            "source_kind": e.source_kind, "target_kind": e.target_kind}
                           for e in edges if e.source_key != "nodes"]}

        return {
            "job_id": job_id,
            "status": job.status,
            "report_ready": True,
            "input_kind": job.input_kind,
            "input_label": job.input_label,
            "duration_s": (summary or {}).get("duration_s"),
            "content": {
                "content_type": content.content_type,
                "title": content.title,
                "author": content.author,
                "publisher": content.publisher,
                "published_at": content.published_at.isoformat() if content.published_at else None,
                "updated_at": content.updated_at.isoformat() if content.updated_at else None,
                "original_url": content.original_url,
                "canonical_url": content.canonical_url,
                "redirect_chain": json.loads(content.redirect_chain) if content.redirect_chain else [],
                "og_metadata": json.loads(content.og_metadata) if content.og_metadata else {},
                "fetch_status": content.fetch_status,
                "http_status": content.http_status,
                "raw_text": content.raw_text,
                "text_stats": json.loads(content.text_stats) if content.text_stats else {},
                "file_meta": json.loads(content.file_meta) if content.file_meta else {},
                "source_classification": content.source_classification,
            } if content else None,
            "media": [{
                "media_type": m.media_type, "filename": m.file_name, "sha256": m.sha256,
                "size_bytes": m.size_bytes,
                "analysis": json.loads(m.analysis) if m.analysis else {},
                "ocr_text": m.ocr_text,
                "manipulation_risk": m.manipulation_risk,
                "ai_generation_signal": m.ai_generation_signal,
                "ai_signal_confidence": m.ai_signal_confidence,
                "authenticity_note": m.authenticity_note,
                "detectors_run": json.loads(m.detectors_run) if m.detectors_run else [],
                "source_classification": m.source_classification,
            } for m in media],
            "claims": claims_dto,
            "numerical_checks": [{
                "check_type": n.check_type, "subject": n.subject, "expected": n.expected,
                "observed": n.observed, "status": n.status, "detail": n.detail,
                "source_classification": n.source_classification,
            } for n in numerics],
            "overall": (summary or {}).get("overall") or _overall_from_summary(summary),
            "risk": (summary or {}).get("risk"),
            "priority": (summary or {}).get("priority"),
            "providers": (summary or {}).get("providers"),
            "graph": graph,
            "timeline": [{
                "occurred_at": t.occurred_at.isoformat() if t.occurred_at else None,
                "occurred_at_raw": t.occurred_at_raw,
                "label": t.label, "detail": t.detail, "event_kind": t.event_kind, "url": t.url,
            } for t in timeline],
        }


def _overall_from_summary(summary: Optional[dict]) -> dict[str, Any]:
    if not summary:
        return {}
    return {"verdict": summary.get("verdict"), "detail": summary.get("detail"),
            "caveats": summary.get("caveats", [])}


@router.get("/evidence/providers", summary="Live provider health (honest states, never simulated)")
async def provider_health() -> dict[str, Any]:
    from app.evidence.providers import EvidenceRetriever

    # Every state comes from the provider itself. Keyed providers (google_factcheck,
    # newsapi) run a real minimal API probe, so CONNECTED means the key was accepted —
    # not merely that a key string exists.
    statuses = await EvidenceRetriever().health()
    providers = [{"name": s.name, "state": s.state, "detail": s.detail} for s in statuses]
    return {"providers": providers,
            "note": "DISABLED providers are skipped — results are never fabricated for missing sources."}


@router.get("/factcheck/search", summary="Ad-hoc fact-check lookup for one claim")
async def factcheck_search(
    claim: str = Query(..., min_length=6, max_length=600),
) -> dict[str, Any]:
    from app.evidence.providers import GoogleFactCheckProvider, ZaiWebSearchProvider
    fc_provider = GoogleFactCheckProvider()
    web = ZaiWebSearchProvider()
    fact_checks = await fc_provider.fact_checks(claim, num=5)
    web_hits = await web.search(claim, num=5) if web.is_configured() else []
    response: dict[str, Any] = {
        "claim": claim,
        "fact_checks": fact_checks,
        "web_evidence": [{
            "url": w.url, "title": w.title, "snippet": w.snippet,
            "publisher": w.publisher, "published_at": w.published_at,
        } for w in web_hits],
    }
    if not fact_checks:
        response["note"] = ("No matching external fact-check found. "
                            "The absence of a fact-check must NOT be interpreted as proof that the claim is true.")
    return response


@router.get("/sources/profiles", summary="Source reputation profiles (spec #47)")
def source_profiles(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    with session_scope() as db:
        rows = (db.query(SourceProfile)
                .order_by(SourceProfile.observation_count.desc())
                .limit(limit).all())
        return {"total": len(rows), "profiles": [{
            "host": r.host, "display_name": r.display_name, "quality": r.quality,
            "classification": r.classification, "signals": json.loads(r.signals) if r.signals else [],
            "observation_count": r.observation_count,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        } for r in rows]}

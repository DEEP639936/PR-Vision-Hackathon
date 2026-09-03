"""Verification pipeline orchestrator — the evidence-first flow (spec #3).

    USER INPUT -> CONTENT INGESTION -> NORMALIZATION -> TYPE DETECTION
      -> CLAIM EXTRACTION -> ENTITY/SOURCE IDENTIFICATION
      -> EVIDENCE RETRIEVAL + FACT-CHECK SEARCH
      -> MULTIMODAL ANALYSIS (image/PDF/document forensics)
      -> NUMERICAL CHECKS (deterministic)
      -> EVIDENCE FUSION -> EXPLAINABLE VERDICT(S)
      -> MULTIMODAL RISK -> INTERVENTION PRIORITY

Runs as asyncio background jobs (like the ingestion scheduler); CPU-heavy
media work is pushed to worker threads. Every artifact is persisted so the
report is fully traceable (spec #37) and every datum carries a source
classification (spec #29).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.db.database import session_scope
from app.db.models import (
    AnalyzedContent,
    Claim,
    ClaimVerdict,
    EvidenceItem,
    FactCheckMatch,
    MediaAnalysis,
    NumericalCheck,
    SourceProfile,
    TimelineEvent,
    VerificationJob,
)
from app.evidence.fusion import fuse_claim
from app.evidence.graph import EvidenceGraphBuilder
from app.evidence.providers import EvidenceRetriever, sidecar_healthy
from app.evidence.ranking import rank_source, SourceSignals
from app.media.numeric import NumCheck, check_csv_stats, check_table, run_all_text_checks
from app.verification import ingestion as ing
from app.verification.claims import extract_claims
from app.services.risk_engine import assess_misinformation_risk

logger = get_logger("prvision.verify.service")


def _write_session(fn, *, attempts: int = 5):
    """Run fn(db) inside a short transaction, retrying on SQLite writer contention.

    WAL gives readers non-blocking reads but still allows exactly ONE writer;
    ingestion bursts can hold it briefly. sqlite's busy_timeout covers plain
    waits but NOT BUSY_SNAPSHOT (a deferred transaction that turns into a write
    after another writer has committed fails instantly). Retrying re-runs the
    whole block with fresh transaction state — every block passed here is
    idempotent (pure INSERTs or updates keyed by job_id).
    """
    import random as _rand
    from sqlalchemy.exc import OperationalError as _OpErr

    delay = 0.15
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            with session_scope() as db:
                return fn(db)
        except _OpErr as exc:
            if "database is locked" not in str(exc):
                raise
            last_exc = exc
            logger.warning("DB write contended for verify job (attempt %d/%d) — retrying",
                           attempt + 1, attempts)
            time.sleep(delay * (0.5 + _rand.random()))
            delay = min(1.5, delay * 2)
    raise last_exc  # type: ignore[misc]

_NEGATIVE_VERDICTS = {"CONTRADICTED", "LIKELY_MISLEADING", "MISLEADING", "MIXED_EVIDENCE", "OUTDATED"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VerificationPipeline:
    """Async job runner + staged pipeline."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(2)          # max concurrent jobs
        self._evidence_sem = asyncio.Semaphore(3)  # max concurrent provider fan-outs
        self.retriever = EvidenceRetriever()

    # ------------------------------------------------------------- submit
    async def submit(
        self,
        input_kind: str,
        *,
        url: Optional[str] = None,
        text: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
        filename: Optional[str] = None,
        submitted_by: Optional[str] = None,
    ) -> dict[str, Any]:
        input_kind = (input_kind or "").lower()
        if input_kind not in ing.SUPPORTED_INPUT_KINDS:
            input_kind = ing.detect_input_kind(text or url or "", filename)

        label = url or filename or ((text or "")[:80].replace("\n", " ") + ("…" if len(text or "") > 80 else ""))

        def _insert_job(db):
            job = VerificationJob(
                status="queued", input_kind=input_kind, input_label=(label or "input")[:500],
                submitted_by=submitted_by, stage="queued",
            )
            db.add(job)
            db.flush()
            return job.id

        job_id = _write_session(_insert_job)
        task = asyncio.create_task(self._run(job_id, url=url, text=text,
                                             file_bytes=file_bytes, filename=filename),
                                   name=f"verify-{job_id}")
        self._tasks[job_id] = task
        logger.info("Verification job %s queued (%s)", job_id, input_kind)
        return {"job_id": job_id, "status": "queued"}

    async def cancel(self, job_id: int) -> bool:
        task = self._tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
            with session_scope() as db:
                job = db.get(VerificationJob, job_id)
                if job and job.status in ("queued", "running"):
                    job.status = "cancelled"
                    job.finished_at = _utcnow()
            return True
        return False

    # ------------------------------------------------------------ runner
    async def _run(self, job_id: int, **payload: Any) -> None:
        async with self._sem:
            try:
                def _mark_running(db):
                    job = db.get(VerificationJob, job_id)
                    if not job or job.status == "cancelled":
                        return False
                    job.status = "running"
                    job.started_at = _utcnow()
                    job.stage = "content ingestion"
                    job.progress = 5
                    return True

                if not _write_session(_mark_running):
                    return

                report = await self._pipeline(job_id, **payload)

                def _mark_done(db):
                    job = db.get(VerificationJob, job_id)
                    if job:
                        job.status = "completed"
                        job.progress = 100
                        job.stage = "done"
                        job.finished_at = _utcnow()

                _write_session(_mark_done)
                logger.info("Verification job %s completed in %.1fs", job_id, report.get("duration_s", 0))

                # Alert engine over verification artifacts (spec #13):
                # evidence conflict, media signals, content risk.
                from app.services.alert_engine import evaluate_verification_async
                await evaluate_verification_async(job_id)
            except asyncio.CancelledError:
                def _mark_cancelled(db):
                    job = db.get(VerificationJob, job_id)
                    if job:
                        job.status = "cancelled"
                        job.finished_at = _utcnow()
                try:
                    _write_session(_mark_cancelled)
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("Verification job %s failed", job_id)
                def _mark_failed(db):
                    job = db.get(VerificationJob, job_id)
                    if job:
                        job.status = "failed"
                        job.error = str(exc)[:800]
                        job.finished_at = _utcnow()
                try:
                    _write_session(_mark_failed)
                except Exception:
                    logger.exception("Could not record failure state for job %s", job_id)
            finally:
                self._tasks.pop(job_id, None)

    def _progress(self, job_id: int, pct: int, stage: str) -> None:
        try:
            with session_scope() as db:
                job = db.get(VerificationJob, job_id)
                if job:
                    job.progress = max(job.progress, pct)
                    job.stage = stage
        except Exception:
            pass

    # ----------------------------------------------------------- pipeline
    async def _pipeline(self, job_id: int, *, url: Optional[str] = None, text: Optional[str] = None,
                        file_bytes: Optional[bytes] = None, filename: Optional[str] = None) -> dict[str, Any]:
        started = time.time()
        graph = EvidenceGraphBuilder(f"job:{job_id}")

        # ---------------- stage 1: ingestion + normalization ----------------
        self._progress(job_id, 8, "acquiring content")
        content, media_bundle = await asyncio.to_thread(
            self._ingest_sync, url, text, file_bytes, filename)

        # Fallback chain (spec #5): direct fetch blocked/sparse → sidecar page
        # reader (JS-tolerant, different egress). Still only public content —
        # a blocked page stays a blocked page, honestly reported.
        if url and (content.fetch_status in ("error", "auth_required") or len(content.raw_text or "") < 300):
            try:
                from app.evidence.providers import ZaiPageReader
                from app.verification.ingestion import _extract_article
                page = await ZaiPageReader().read(url)
                if page and len(page.get("html") or "") > 500:
                    _extract_article(content, page["html"], url)
                    content.fetch_status = "ok"
                    content.http_status = content.http_status or 200
                    if page.get("published_time") and not content.published_at:
                        content.published_at = ing.parse_date_guess(page["published_time"])
                    if page.get("title") and not content.title:
                        content.title = page["title"]
                    content.og_metadata["fetch_provider"] = "zai_page_reader"
                    logger.info("job %s: direct fetch fell back to sidecar reader (%d chars)",
                                job_id, len(content.raw_text or ""))
            except Exception:
                logger.info("job %s: sidecar reader fallback failed — keeping honest error state", job_id)

        def _persist_content(db):
            ac = AnalyzedContent(
                job_id=job_id,
                content_type=content.content_type,
                language=None,
                title=(content.title or "")[:500] or None,
                author=(content.author or "")[:500] or None,
                publisher=(content.publisher or "")[:500] or None,
                published_at=content.published_at,
                updated_at=content.updated_at,
                original_url=content.original_url,
                canonical_url=content.canonical_url,
                redirect_chain=json.dumps(content.redirect_chain)[:8000] if content.redirect_chain else None,
                og_metadata=json.dumps(content.og_metadata, ensure_ascii=False)[:12000] if content.og_metadata else None,
                fetch_status=content.fetch_status,
                http_status=content.http_status,
                raw_text=content.raw_text,
                text_stats=json.dumps(content.text_stats, ensure_ascii=False)[:4000],
                file_meta=json.dumps(content.file_meta, ensure_ascii=False)[:4000],
                source_classification=content.source_classification,
            )
            db.add(ac)
            db.flush()
            return ac.id

        content_id = _write_session(_persist_content)
        content_key = graph.add_content(content.title or content.original_url or "Submitted content",
                                        content.original_url or content.canonical_url, content.content_type)

        # ---------------- stage 2: media forensics (if media) ---------------
        media_result: Optional[dict[str, Any]] = None
        if media_bundle:
            self._progress(job_id, 20, "media forensics")
            try:
                media_result = await self._media_analysis(job_id, content_id, media_bundle, content, graph)
            except Exception:
                logger.exception("media analysis failed for job %s (continuing without)", job_id)

        # OCR text from images becomes claim source too (spec #15/#16)
        claim_source_text = content.raw_text or ""
        if media_result and media_result.get("ocr_text") and len(claim_source_text) < 200:
            claim_source_text = media_result["ocr_text"]

        # ---------------- stage 3: claim extraction -------------------------
        self._progress(job_id, 32, "extracting claims")
        claims = await extract_claims(claim_source_text) if claim_source_text else []
        def _persist_claims(db):
            rows: list[tuple[int, int]] = []  # (id, ordinal)
            for cl in claims:
                row = Claim(
                    job_id=job_id, content_id=content_id,
                    ordinal=cl.ordinal, text=cl.text[:4000],
                    claim_type=cl.claim_type, checkable=cl.checkable,
                    claim_confidence=cl.confidence,
                    time_context=cl.time_context,
                    entities=json.dumps(cl.entities, ensure_ascii=False)[:4000] if cl.entities else None,
                    numbers=json.dumps(cl.numbers, ensure_ascii=False)[:4000] if cl.numbers else None,
                    extraction_method=cl.method,
                )
                db.add(row)
                db.flush()
                rows.append((row.id, cl.ordinal))
            return rows

        claim_rows = _write_session(_persist_claims)
        claim_keys: dict[int, str] = {}
        for cid, ordinal in claim_rows:
            cl = next(c for c in claims if c.ordinal == ordinal)
            claim_keys[ordinal] = graph.add_claim(ordinal, cl.text, cl.claim_type, "PENDING", cl.confidence)
            graph.wire_entities(claim_keys[ordinal], cl.entities)
        graph.wire_content_claims(content_key, list(claim_keys.values()))

        # ---------------- stage 4: evidence retrieval + fact-checks ---------
        self._progress(job_id, 45, "retrieving evidence & fact-checks")
        checkable = [cl for cl in claims if cl.checkable]
        evidence_by_claim: dict[int, list[dict[str, Any]]] = {}
        factchecks_by_claim: dict[int, list[dict[str, Any]]] = {}
        general_evidence: list[dict[str, Any]] = []

        sidecar_ok = await sidecar_healthy()
        if not sidecar_ok:
            logger.info("provider sidecar unavailable — evidence retrieval degrades to keyless providers")

        async def _one_claim(cl):
            async with self._evidence_sem:
                try:
                    ev, fc = await self.retriever.retrieve_for_claim(
                        cl.text, cl.entities, num_per_provider=3)
                    return cl.ordinal, ev, fc
                except Exception:
                    logger.exception("evidence retrieval failed for claim %s", cl.ordinal)
                    return cl.ordinal, [], []

        tasks = [_one_claim(cl) for cl in checkable[: settings.VERIFY_MAX_CLAIMS]]
        # job-level context evidence (uses title/entities)
        topic = " ".join(filter(None, [content.title, (content.publisher or "")])) or (claim_source_text[:120] if claim_source_text else "")

        async def _general_task() -> tuple[int, list, list]:
            async with self._evidence_sem:
                ev = await self.retriever.retrieve_general(topic, num=5)
                return -1, list(ev), []

        if topic:
            tasks.append(_general_task())
        general_evidence: list[dict[str, Any]] = []
        for ordinal, ev_list, fc_list in await asyncio.gather(*tasks, return_exceptions=False):
            if ordinal == -1:
                general_evidence = list(ev_list)
                continue
            evidence_by_claim[ordinal] = list(ev_list)
            factchecks_by_claim[ordinal] = fc_list

        # ---------------- stage 5: numerical checks -------------------------
        self._progress(job_id, 60, "numerical verification")
        num_checks: list[NumCheck] = []
        if claim_source_text:
            num_checks += await asyncio.to_thread(run_all_text_checks, claim_source_text)
        if content.content_type == "csv" and content.text_stats.get("sample_rows"):
            rows = [[str(c) for c in r] for r in content.text_stats.get("sample_rows", [])]
            num_checks += check_table(rows, content.text_stats.get("header"))
            num_checks += check_csv_stats(content.text_stats)
        if media_result and media_result.get("ocr_text"):
            num_checks += await asyncio.to_thread(run_all_text_checks, media_result["ocr_text"])
        def _persist_num_checks(db):
            for idx, chk in enumerate(num_checks[:40]):
                claim_id = None
                ordinal_by_id = dict(claim_rows)
                id_by_ordinal = {o: i for i, o in ordinal_by_id.items()}
                if chk.subject:
                    m = re.search(r"claim\s*#?(\d+)", chk.subject, re.IGNORECASE)
                    if m and int(m.group(1)) in id_by_ordinal:
                        claim_id = id_by_ordinal[int(m.group(1))]
                db.add(NumericalCheck(
                    job_id=job_id, claim_id=claim_id,
                    check_type=chk.check_type, subject=(chk.subject or "")[:500],
                    expected=(chk.expected or "")[:120], observed=(chk.observed or "")[:120],
                    status=chk.status, detail=(chk.detail or "")[:2000],
                    source_classification="DERIVED",
                ))

        _write_session(_persist_num_checks)

        # ---------------- stage 6: fusion per claim + persistence -----------
        self._progress(job_id, 72, "fusing evidence")
        assessments: list[dict[str, Any]] = []
        verdict_by_ordinal: dict[int, dict[str, Any]] = {}

        for cl in claims:
            if not cl.checkable:
                verdict_by_ordinal[cl.ordinal] = {
                    "verdict": "SATIRE/PARODY" if cl.claim_type == "SATIRE" else "UNVERIFIED",
                    "confidence": 0.4 if cl.claim_type == "SATIRE" else 0.3,
                    "supporting": 0, "contradicting": 0, "neutral": 0,
                    "primary": False, "temporal": None,
                    "explanation": f"Claim type {cl.claim_type} is not fact-checkable by design; no evidence fusion applied.",
                    "fused": {},
                    "evidence": [], "fact_checks": [],
                    "checkable": False,
                }
                continue
            ev_payloads = []
            for obj in evidence_by_claim.get(cl.ordinal, []):
                ev_payloads.append({
                    "provider": obj.provider, "url": obj.url, "title": obj.title,
                    "snippet": obj.snippet, "publisher": obj.publisher,
                    "published_at": obj.published_at, "relevance": obj.relevance,
                })
            assessment = await asyncio.to_thread(
                fuse_claim, cl.text, cl.claim_type, cl.time_context,
                ev_payloads, factchecks_by_claim.get(cl.ordinal, []))
            verdict_by_ordinal[cl.ordinal] = {
                "verdict": assessment.verdict,
                "confidence": assessment.confidence,
                "supporting": assessment.supporting,
                "contradicting": assessment.contradicting,
                "neutral": assessment.neutral,
                "primary": assessment.primary_source_available,
                "temporal": assessment.temporal_flag,
                "explanation": assessment.explanation,
                "fused": assessment.fused_signals,
                "confidence_rationale": assessment.confidence_rationale,
                "evidence": [s.to_dict() for s in assessment.evidence],
                "fact_checks": assessment.fact_checks,
                "checkable": True,
            }
            graph.wire_claim_evidence(claim_keys.get(cl.ordinal, f"claim:{cl.ordinal}"),
                                      assessment.evidence, max_per_claim=6)
            graph.wire_fact_check(claim_keys.get(cl.ordinal, f"claim:{cl.ordinal}"),
                                  assessment.fact_checks, cl.ordinal)

            assessments.append({
                "ordinal": cl.ordinal, "checkable": True,
                "verdict": assessment.verdict, "confidence": assessment.confidence,
            })

        # persist verdicts + evidence + fact-checks + source profiles
        def _persist_verdicts(db):
            id_by_ordinal = {o: i for i, o in claim_rows}
            _profile_cache: dict[str, SourceProfile] = {}  # host -> row (dedupes this session)
            for cl in claims:
                v = verdict_by_ordinal.get(cl.ordinal)
                if not v:
                    continue
                cid = id_by_ordinal[cl.ordinal]
                db.add(ClaimVerdict(
                    claim_id=cid, verdict=v["verdict"], confidence=v["confidence"],
                    confidence_rationale=(v.get("confidence_rationale") or "")[:2000] or None,
                    supporting_count=v["supporting"], contradicting_count=v["contradicting"],
                    neutral_count=v["neutral"], primary_source_available=bool(v["primary"]),
                    temporal_flag=v["temporal"], explanation=(v["explanation"] or "")[:4000],
                    fused_signals=json.dumps(v["fused"], ensure_ascii=False)[:6000],
                ))
                seen_urls: set[str] = set()
                for s in v["evidence"][: settings.VERIFY_MAX_EVIDENCE_PER_CLAIM]:
                    if s.get("url") and s["url"] in seen_urls:
                        continue
                    seen_urls.add(s.get("url") or "")
                    db.add(EvidenceItem(
                        job_id=job_id, claim_id=cid,
                        provider=s.get("provider", "web"),
                        url=(s.get("url") or "")[:2000] or None,
                        title=(s.get("title") or "")[:500] or None,
                        snippet=s.get("snippet"),
                        publisher=(s.get("publisher") or "")[:240] or None,
                        published_at=s.get("published_at"),
                        stance=s.get("stance", "neutral"),
                        stance_confidence=s.get("stance_confidence", 0.5),
                        relevance=s.get("relevance", 0.0),
                        quality=s.get("quality"),
                        source_classification="EXTERNAL_EVIDENCE",
                        independence_cluster=s.get("independence_key"),
                    ))
                    host = (s.get("url") or "").split("/")[2] if "://" in (s.get("url") or "") else None
                    if host and s.get("quality") is not None:
                        normalized = host.lower().removeprefix("www.")
                        # get-or-create with an in-session cache: pending INSERTs
                        # are not visible to a query before flush, which caused a
                        # UNIQUE(host) crash when one job cites the same host twice.
                        prof = _profile_cache.get(normalized)
                        if prof is None:
                            prof = db.query(SourceProfile).filter(SourceProfile.host == normalized).one_or_none()
                        if prof is None:
                            prof = SourceProfile(host=normalized,
                                                 display_name=s.get("publisher"), quality=s["quality"],
                                                 signals=json.dumps(s.get("signals", []))[:6000],
                                                 classification=s.get("classification"),
                                                 observation_count=1, last_seen_at=_utcnow())
                            db.add(prof)
                        else:
                            prof.quality = round(prof.quality * 0.7 + s["quality"] * 0.3, 3)
                            prof.observation_count += 1
                            prof.last_seen_at = _utcnow()
                        _profile_cache[normalized] = prof
                for fc in v["fact_checks"][:4]:
                    db.add(FactCheckMatch(
                        job_id=job_id, claim_id=cid,
                        provider=fc.get("provider", "google_factcheck"),
                        claim_text=fc.get("claim_text"),
                        textual_rating=(fc.get("textual_rating") or "")[:120],
                        publisher=(fc.get("publisher") or "")[:240],
                        published_at=fc.get("published_at"),
                        url=(fc.get("url") or "")[:2000],
                        review_snippet=fc.get("snippet"),
                        source_classification="EXTERNAL_EVIDENCE",
                    ))

        _write_session(_persist_verdicts)

        # ---------------- stage 7: risk + overall verdict + priority --------
        self._progress(job_id, 84, "risk engine & verdict")
        evidence_qualities = [s.get("quality") or 0.0 for v in verdict_by_ordinal.values() for s in v.get("evidence", [])]
        overall = self._overall_verdict(assessments, verdict_by_ordinal, media_result, content)

        risk = await asyncio.to_thread(
            assess_misinformation_risk,
            claim_source_text, content.text_stats, assessments,
            evidence_qualities, media_result, None,
            [c.to_dict() for c in num_checks])

        priority = self._intervention_priority(risk, overall, evidence_qualities)

        # ---------------- stage 8: timeline + graph persistence -------------
        self._progress(job_id, 92, "building evidence graph & timeline")
        events: list[dict[str, Any]] = []
        if content.published_at:
            events.append({"occurred_at": content.published_at, "label": "Content published",
                           "detail": f"{content.publisher or 'publisher'} — {content.title or ''}",
                           "kind": "publication", "url": content.original_url})
        for v in verdict_by_ordinal.values():
            for fc in v.get("fact_checks", []):
                if fc.get("published_at") or fc.get("url"):
                    events.append({"occurred_at": None, "occurred_at_raw": fc.get("published_at"),
                                   "label": f"Fact-check: {fc.get('textual_rating') or 'review'}",
                                   "detail": f"{fc.get('publisher') or ''} — {(fc.get('claim_text') or '')[:160]}",
                                   "kind": "fact_check", "url": fc.get("url")})
            for s in v.get("evidence", [])[:3]:
                if s.get("published_at"):
                    events.append({"occurred_at": None, "occurred_at_raw": s.get("published_at"),
                                   "label": f"Evidence published ({s.get('stance', 'neutral')})",
                                   "detail": f"{s.get('publisher') or ''} — {(s.get('title') or '')[:160]}",
                                   "kind": "evidence", "url": s.get("url")})
        events.append({"occurred_at": _utcnow(), "label": "PR•VISION verification completed",
                       "detail": f"{len(claims)} claims, {len(evidence_qualities)} evidence items, verdict {overall['verdict']}",
                       "kind": "detection", "url": None})

        edge_count = len(graph.edges)
        def _persist_timeline(db):
            for ev_row in events[:60]:
                db.add(TimelineEvent(
                    job_id=job_id,
                    occurred_at=ev_row.get("occurred_at"),
                    occurred_at_raw=(ev_row.get("occurred_at_raw") or "")[:120] or None,
                    label=(ev_row.get("label") or "")[:240],
                    detail=ev_row.get("detail"),
                    event_kind=ev_row.get("kind", "evidence"),
                    url=(ev_row.get("url") or "")[:2000] or None,
                ))
            # graph nodes serialized once; edges as rows
            from app.db.models import EvidenceEdge
            for e in graph.edges:
                sn = graph.nodes.get(e.source)
                tn = graph.nodes.get(e.target)
                db.add(EvidenceEdge(
                    job_id=job_id,
                    source_kind=sn.kind if sn else "url", source_key=e.source,
                    edge_type=e.edge_type,
                    target_kind=tn.kind if tn else "url", target_key=e.target,
                    weight=e.weight, note=e.note,
                ))
            db.add(EvidenceEdge(
                job_id=job_id, source_kind="meta", source_key="nodes",
                edge_type="graph_nodes", target_kind="meta", target_key=content_key,
                weight=0.0,
                note=json.dumps([n.to_dict() for n in graph.nodes.values()])[:4_000_000],
            ))

        _write_session(_persist_timeline)

        report = {
            "job_id": job_id,
            "duration_s": round(time.time() - started, 1),
            "content": content.to_summary(),
            "media": media_result,
            "claims": [
                {**cl.to_dict(), **(verdict_by_ordinal.get(cl.ordinal) or {})}
                for cl in claims
            ],
            "numerical_checks": [c.to_dict() for c in num_checks],
            "overall": overall,
            "risk": risk,
            "priority": priority,
            "graph": graph.to_dict(),
            "timeline": events,
            "providers": {
                "sidecar": "CONNECTED" if sidecar_ok else "UNAVAILABLE",
                "google_factcheck": "CONNECTED" if self.retriever.factcheck.is_configured() else "DISABLED",
                "note": "Evidence retrieval ran through configured providers only. Disabled providers were NOT simulated.",
            },
        }
        # persist compact header for queue/list views (full report is rebuilt
        # from per-artifact tables by the API for traceability)
        summary = {
            "verdict": overall["verdict"], "detail": overall["detail"],
            "caveats": overall.get("caveats", []),
            "risk": risk, "priority": priority, "providers": report["providers"],
            "claims_total": len(claims),
            "claims_checkable": sum(1 for a in assessments if a.get("checkable")),
            "evidence_count": len(evidence_qualities),
            "duration_s": report["duration_s"],
        }
        def _persist_summary(db):
            job = db.get(VerificationJob, job_id)
            if job:
                job.stage = "done"
                job.result_summary = json.dumps(summary, ensure_ascii=False, default=str)[:100_000]

        _write_session(_persist_summary)
        return report

    # ---------------------------------------------------------- ingestion
    def _ingest_sync(self, url, text, file_bytes, filename) -> tuple[ing.IngestedContent, Optional[dict]]:
        """Synchronous acquisition (runs in worker thread). Returns content + media bundle."""
        media_bundle: Optional[dict[str, Any]] = None
        kind = ing.detect_input_kind(text or "", filename) if not (url or file_bytes) else (
            "url" if url else ing.detect_input_kind("", filename))

        if url:
            content = ing.fetch_url(url)
            if content.input_kind == "pdf":
                data = ing.pop_stashed_bytes(content)
                if data:
                    content, parsed = ing.parse_pdf_bytes(data, filename or "document.pdf")
                    media_bundle = {"media_type": "pdf", "bytes": data, "parsed": parsed, "filename": filename or "document.pdf"}
            elif content.input_kind == "image":
                data = ing.pop_stashed_bytes(content)
                if data:
                    media_bundle = {"media_type": "image", "bytes": data, "filename": filename or "image"}
            return content, media_bundle

        if file_bytes:
            fname = filename or "upload"
            ext = Path(fname).suffix.lower()
            if ext == ".pdf" or kind == "pdf":
                content, parsed = ing.parse_pdf_bytes(file_bytes, fname)
                media_bundle = {"media_type": "pdf", "bytes": file_bytes, "parsed": parsed, "filename": fname}
                return content, media_bundle
            if ext == ".doc":
                # Legacy OLE2 .doc (or RTF/HTML mislabeled as .doc) — python-docx
                # can only read real .docx; route by actual bytes, honestly.
                if file_bytes[:4] == b"PK\x03\x04":
                    content, parsed = ing.parse_docx_bytes(file_bytes, fname)
                    return content, {"media_type": "docx", "bytes": file_bytes, "parsed": parsed, "filename": fname}
                # parse_legacy_doc_bytes returns (content, parsed); pipeline
                # expects (content, media_bundle) — legacy .doc has no media stage.
                legacy_content, _legacy_parsed = ing.parse_legacy_doc_bytes(file_bytes, fname)
                return legacy_content, None
            if ext == ".docx" or kind == "docx":
                content, parsed = ing.parse_docx_bytes(file_bytes, fname)
                return content, {"media_type": "docx", "bytes": file_bytes, "parsed": parsed, "filename": fname}
            if ext == ".csv" or ext == ".tsv" or kind == "csv":
                content, stats = ing.parse_csv_bytes(file_bytes, fname)
                return content, {"media_type": "csv", "bytes": file_bytes, "parsed": stats, "filename": fname}
            if ext == ".json" or kind == "json":
                return ing.parse_json_bytes(file_bytes, fname), None
            if ext in (".html", ".htm") or kind == "html":
                return ing.parse_html_bytes(file_bytes, fname), None
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff") or kind in ("image", "screenshot"):
                c = ing.IngestedContent(
                    input_kind=kind or "image", content_type=kind or "image",
                    title=fname, fetch_status="skipped",
                    file_meta={"size": len(file_bytes), "sha256": hashlib.sha256(file_bytes).hexdigest()})
                return c, {"media_type": "image", "bytes": file_bytes, "filename": fname}
            # fallback: treat as text
            return ing.text_from_plain(file_bytes.decode("utf-8-sig", errors="replace")), None

        return ing.text_from_plain(text or ""), None

    # ------------------------------------------------------------- media
    async def _media_analysis(self, job_id: int, content_id: int, bundle: dict, content: ing.IngestedContent, graph: EvidenceGraphBuilder) -> dict[str, Any]:
        mtype = bundle["media_type"]
        data = bundle["bytes"]
        filename = bundle.get("filename", "media")
        if mtype == "image":
            # optional vision signal (sidecar) as auxiliary detector
            vision_text: Optional[str] = None
            if await sidecar_healthy():
                try:
                    import base64
                    import httpx as _httpx
                    b64 = "data:image/png;base64," + base64.b64encode(data[: 6 * 1024 * 1024]).decode()
                    async with _httpx.AsyncClient(timeout=settings.SIDECAR_TIMEOUT_SECONDS) as client:
                        resp = await client.post(f"{settings.SIDECAR_URL.rstrip('/')}/vision", json={
                            "image_url": b64,
                            "text": ("You are a media-forensics assistant. In <=120 words: describe what this image shows; "
                                     "note any text/UI inconsistencies, signs of editing, or AI-generation indicators. "
                                     "Be specific and conservative; say 'no strong indicators' if none."),
                        })
                        d = resp.json()
                        if d.get("ok"):
                            vision_text = (d.get("data") or {}).get("text")
                except Exception:
                    vision_text = None
            result = await asyncio.to_thread(_analyze_image_sync, data, filename, vision_text)
        elif mtype == "pdf":
            parsed = bundle.get("parsed") or {}
            result = {
                "media_type": "pdf", "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                "pages": parsed.get("pages"), "images": parsed.get("images"),
                "links_count": len(parsed.get("links") or []),
                "headings": (parsed.get("headings") or [])[:12],
                "forensics": parsed.get("forensics") or [],
                "metadata": {k: parsed.get(k) for k in ("title", "author", "creator", "producer",
                                                        "creation_date", "mod_date")},
                "detectors_run": ["pypdf_metadata", "pymupdf_structure", "pdf_forensics"],
                "authenticity_note": "Heuristic document forensics — anomalies are review signals, not proof of fraud (spec #20).",
                "manipulation_risk": round(min(0.5, 0.12 * len(parsed.get("forensics") or [])), 3),
            }
        elif mtype == "docx":
            parsed = bundle.get("parsed") or {}
            result = {
                "media_type": "docx", "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                "paragraphs": parsed.get("paragraphs"), "tables": parsed.get("tables"),
                "metadata": {k: parsed.get(k) for k in ("title", "author", "created", "modified")},
                "detectors_run": ["python_docx"],
                "authenticity_note": "Structure extracted; content claims verified via the claim pipeline.",
                "manipulation_risk": 0.0,
            }
        elif mtype == "csv":
            result = {
                "media_type": "csv", "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                "structure": bundle.get("parsed"),
                "detectors_run": ["csv_structure"],
                "authenticity_note": "Numeric columns queued for deterministic table verification.",
                "manipulation_risk": 0.0,
            }
        else:
            return None

        # persist MediaAnalysis row
        def _persist_media(db):
            db.add(MediaAnalysis(
                job_id=job_id, content_id=content_id,
                media_type=mtype, file_name=(filename or "")[:500],
                sha256=result.get("sha256"), size_bytes=result.get("size_bytes"),
                analysis=json.dumps(result, ensure_ascii=False, default=str)[:400_000],
                ocr_text=(result.get("ocr_text") or None),
                manipulation_risk=result.get("manipulation_risk"),
                ai_generation_signal=result.get("ai_generation_signal"),
                ai_signal_confidence=result.get("ai_signal_confidence"),
                authenticity_note=result.get("authenticity_note"),
                detectors_run=json.dumps(result.get("detectors_run", [])),
                source_classification="DERIVED",
            ))

        _write_session(_persist_media)
        if mtype == "image":
            graph.wire_media("content:main", filename or "Uploaded image", {"media_type": "image",
                                                                            "sha256": result.get("sha256"),
                                                                            "manipulation_risk": result.get("manipulation_risk"),
                                                                            "ai_generation_signal": result.get("ai_generation_signal")})
        return result

    # ------------------------------------------------------------ verdicts
    @staticmethod
    def _overall_verdict(assessments: list[dict], verdict_by_ordinal: dict, media_result, content: ing.IngestedContent) -> dict[str, Any]:
        """Job-level verdict = transparent aggregation of per-claim verdicts."""
        checkable = [a for a in assessments if a.get("checkable")]
        satire = any(v.get("verdict") == "SATIRE/PARODY" for v in verdict_by_ordinal.values())
        if not checkable:
            verdict = "SATIRE/PARODY" if satire else "UNVERIFIED"
            negative_share, support_share = 0.0, 0.0
            detail = "No checkable factual claims were extracted."
        else:
            neg = sum(float(a["confidence"]) for a in checkable if a["verdict"] in _NEGATIVE_VERDICTS)
            strong_neg = sum(float(a["confidence"]) for a in checkable if a["verdict"] in ("CONTRADICTED", "LIKELY_MISLEADING", "MISLEADING"))
            sup = sum(float(a["confidence"]) for a in checkable if a["verdict"] in ("SUPPORTED", "LIKELY_SUPPORTED"))
            total = sum(float(a["confidence"]) for a in checkable) or 1.0
            negative_share, support_share = neg / total, sup / total
            strong_share = strong_neg / total
            if strong_share >= 0.5:
                verdict = "LIKELY_MISLEADING"
            elif negative_share >= 0.45:
                verdict = "MIXED_EVIDENCE"
            elif strong_share > 0:
                verdict = "MIXED_EVIDENCE"
            elif support_share >= 0.5 and negative_share < 0.1:
                verdict = "LIKELY_SUPPORTED"
            elif satire:
                verdict = "SATIRE/PARODY"
            else:
                verdict = "UNVERIFIED"
            detail = (f"{len(checkable)} checkable claims: negative-weighted share {negative_share:.0%}, "
                      f"support-weighted share {support_share:.0%}.")
        if satire and verdict not in ("SATIRE/PARODY",):
            detail += " Satire markers detected — read content as parody risk."
        notes = []
        if media_result and media_result.get("manipulation_risk", 0) and media_result["manipulation_risk"] >= 0.3:
            notes.append("media forensics flagged manipulation indicators")
        if content.fetch_status in ("auth_required", "error"):
            notes.append(f"content acquisition incomplete ({content.fetch_status}) — assessment based on partial input")
        return {
            "verdict": verdict,
            "detail": detail,
            "caveats": notes,
            "content_fetch_status": content.fetch_status,
        }

    @staticmethod
    def _intervention_priority(risk: dict, overall: dict, evidence_qualities: list[float]) -> dict[str, Any]:
        """Explainable Intervention Priority 0-100 (spec #1 Q5 / #33)."""
        r = float(risk.get("misinformation_risk", 0)) / 100.0
        conf = float(risk.get("confidence", 0)) / 100.0
        # evidence conflict and negative verdicts already inside r; confidence modulates
        priority = round(min(100.0, max(0.0, (0.75 * r + 0.25 * conf) * 100.0)), 1)
        label = ("CRITICAL" if priority >= 75 else "HIGH" if priority >= 50 else
                 "MEDIUM" if priority >= 28 else "LOW")
        factors = [
            {"factor": "misinformation risk (fused)", "contribution": round(0.75 * r * 100, 1)},
            {"factor": "assessment confidence", "contribution": round(0.25 * conf * 100, 1)},
        ]
        return {"intervention_priority": priority, "label": label, "factors": factors}


def _analyze_image_sync(data: bytes, filename: str, vision_text: Optional[str]) -> dict:
    from app.media.image_analysis import analyze_image
    result = analyze_image(data, filename, vision_describe=vision_text)
    return result.to_dict()


# Singleton pipeline (like the ingestion scheduler)
pipeline = VerificationPipeline()

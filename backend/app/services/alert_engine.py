"""Alert engine (spec #13) — automatic early-warning alerts.

Five triggers, evaluated honestly from OBSERVED data:

    misinfo_risk        early-warning misinfo score crosses HIGH/CRITICAL
    acceleration_spike  share acceleration + velocity explode together
    forecast_jump       XGBoost prediction jumps sharply vs its predecessor
    evidence_conflict   verification job shows strong supporting AND
                        contradicting evidence for the same claims
    media_signal        media forensics flags manipulation / AI-generation

Severity: LOW | MEDIUM | HIGH | CRITICAL. Repeat alerts for the same
condition are suppressed via dedupe_key + a time window. Alerts are always
derived from stored scores/analyses — never fabricated.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Claim,
    ClaimVerdict,
    FeatureSnapshot,
    InterventionScore,
    MediaAnalysis,
    MisinformationScore,
    Prediction,
    Post,
    VerificationJob,
)
from app.db.repositories import AlertRepository, PredictionRepository

logger = get_logger("prvision.alerts")

# documented thresholds (aligned with risk_engine / scoring_service)
MISINFO_HIGH = 0.75
MISINFO_CRITICAL = 0.90
ACCELERATION_SPIKE = 0.50          # shares/min²
ACCELERATION_VELOCITY_FLOOR = 5.0  # shares/min — spike must occur while spreading
FORECAST_JUMP_MIN_SHARES = 300.0
FORECAST_JUMP_RATIO = 2.5
MEDIA_MANIPULATION_ALERT = 0.60
AI_SIGNAL_ALERT_CONFIDENCE = 0.60
_CONFLICT_VERDICTS = {"MIXED_EVIDENCE", "MISLEADING", "CONTRADICTED"}


def _severity_from_risk(risk: float) -> str:
    if risk >= MISINFO_CRITICAL:
        return "CRITICAL"
    if risk >= MISINFO_HIGH:
        return "HIGH"
    if risk >= 0.5:
        return "MEDIUM"
    return "LOW"


def _raise(db: Session, *, kind: str, severity: str, title: str, message: str,
           metrics: dict[str, Any] | None, post_id: int | None = None,
           verification_job_id: int | None = None, dedupe_key: str | None = None) -> bool:
    """Create an alert unless the same condition fired recently. True if raised."""
    if severity not in ("MEDIUM", "HIGH", "CRITICAL"):
        return False
    if AlertRepository.recent_duplicate(db, dedupe_key or ""):
        return False
    AlertRepository.add(
        db, kind=kind, severity=severity, title=title[:255], message=message[:2000],
        metrics=metrics or {}, post_id=post_id, verification_job_id=verification_job_id,
        dedupe_key=(dedupe_key or "")[:180] or None)
    logger.info("ALERT [%s] %s (post=%s job=%s)", severity, kind, post_id, verification_job_id)
    return True


# ------------------------------------------------------------ early-warning path
def evaluate_post_sync(db: Session, post: Post) -> int:
    """Run early-warning triggers for one monitored post. Returns alerts raised."""
    raised = 0

    features = db.execute(
        select_latest(FeatureSnapshot, FeatureSnapshot.post_id, post.id)).scalars().first()
    misinfo = db.execute(
        select_latest(MisinformationScore, MisinformationScore.post_id, post.id)).scalars().first()
    prediction = PredictionRepository.latest_for_post(db, post.id, horizon_minutes=60)
    priority = db.execute(
        select_latest(InterventionScore, InterventionScore.post_id, post.id)).scalars().first()

    # 1) misinformation risk crosses HIGH / CRITICAL
    if misinfo is not None:
        sev = _severity_from_risk(float(misinfo.risk_score))
        if sev in ("HIGH", "CRITICAL", "MEDIUM") and float(misinfo.risk_score) >= 0.5:
            raised += _raise(
                db,
                kind="misinfo_risk",
                severity="HIGH" if sev == "MEDIUM" else sev,
                title=f"Misinformation risk {sev.lower()} on {post.platform} post {post.external_post_id}",
                message=(f"Early-warning misinformation risk is {misinfo.risk_score:.2f} "
                         f"({misinfo.risk_label.lower()}) for post {post.external_post_id}. "
                         "This is a stylistic estimate — open the post for evidence-linked detail."),
                metrics={"risk_score": misinfo.risk_score, "risk_label": misinfo.risk_label,
                         "platform": post.platform, "priority": priority.intervention_priority if priority else None},
                post_id=post.id,
                dedupe_key=f"misinfo_risk:{post.id}:{misinfo.risk_label}")

    # 2) acceleration spike (velocity must be non-trivial, else it's noise)
    accel = getattr(features, "share_acceleration", None)
    velocity = getattr(features, "share_velocity", None)
    if accel is not None and velocity is not None:
        if float(accel) >= ACCELERATION_SPIKE and float(velocity) >= ACCELERATION_VELOCITY_FLOOR:
            sev = "CRITICAL" if accel >= 2.0 else "HIGH" if accel >= 1.0 else "MEDIUM"
            raised += _raise(
                db,
                kind="acceleration_spike",
                severity=sev,
                title=f"Share acceleration spike on {post.platform} post {post.external_post_id}",
                message=(f"Spread is accelerating: +{float(accel):.2f} shares/min² at "
                         f"{float(velocity):.1f} shares/min. Rapid acceleration often precedes "
                         "viral misinformation cascades."),
                metrics={"share_acceleration": accel, "share_velocity": velocity,
                         "platform": post.platform},
                post_id=post.id,
                dedupe_key=f"accel_spike:{post.id}")

    # 3) forecast jump vs previous prediction
    if prediction is not None and prediction.prediction_type == "model":
        previous = db.execute(
            select_previous(Prediction, Prediction.post_id, post.id,
                            Prediction.prediction_timestamp, prediction.prediction_timestamp,
                            horizon=prediction.horizon_minutes)).scalars().first()
        now_pred = float(prediction.predicted_additional_shares)
        if previous is not None and now_pred >= FORECAST_JUMP_MIN_SHARES:
            prev_pred = float(previous.predicted_additional_shares)
            if prev_pred > 1 and now_pred / prev_pred >= FORECAST_JUMP_RATIO:
                sev = "HIGH" if now_pred >= 800 else "MEDIUM"
                raised += _raise(
                    db,
                    kind="forecast_jump",
                    severity=sev,
                    title=f"Forecast jump (+{now_pred - prev_pred:,.0f} shares) on post {post.external_post_id}",
                    message=(f"XGBoost now predicts {now_pred:,.0f} additional shares/"
                             f"{int(prediction.horizon_minutes)}m — up from {prev_pred:,.0f} "
                             f"({now_pred / prev_pred:.1f}×). Forecast confidence "
                             f"{prediction.confidence:.0%}. Predictions are estimates, not certainties."),
                    metrics={"horizon_minutes": prediction.horizon_minutes,
                             "predicted_now": now_pred, "predicted_previous": prev_pred,
                             "confidence": prediction.confidence, "model_version": prediction.model_version},
                    post_id=post.id,
                    dedupe_key=f"forecast_jump:{post.id}:{prediction.horizon_minutes}")

    return raised


def evaluate_early_warning_sync(db: Session, limit: int = 100) -> int:
    """Evaluate triggers for all monitored posts (called after re-scoring)."""
    from app.db.repositories import PostRepository
    posts, _ = PostRepository.list_posts(db, limit=limit, offset=0)
    raised = 0
    for post in posts:
        try:
            raised += evaluate_post_sync(db, post)
        except Exception:
            logger.exception("Alert evaluation failed for post %s", post.id)
    return raised


# ------------------------------------------------------------------ verification path
def evaluate_verification_sync(db: Session, job_id: int) -> int:
    """Triggers over a finished verification job: conflicting evidence,
    suspicious media signals, high content risk."""
    import json

    raised = 0
    job = db.get(VerificationJob, job_id)
    if job is None or job.status != "completed":
        return 0

    # 4) evidence conflict — claims with BOTH supporting and contradicting evidence
    verdicts = (db.query(ClaimVerdict)
                .join(Claim, Claim.id == ClaimVerdict.claim_id)
                .filter(Claim.job_id == job_id).all())
    conflicting = [v for v in verdicts
                   if v.supporting_count >= 1 and v.contradicting_count >= 1]
    strong_conflicts = [v for v in conflicting
                        if v.supporting_count + v.contradicting_count >= 3
                        and v.verdict in _CONFLICT_VERDICTS]
    if strong_conflicts:
        worst = max(strong_conflicts, key=lambda v: v.contradicting_count + v.supporting_count)
        sev = "HIGH" if len(strong_conflicts) >= 2 else "MEDIUM"
        raised += _raise(
            db,
            kind="evidence_conflict",
            severity=sev,
            title=f"Conflicting evidence across sources (job {job_id})",
            message=(f"{len(strong_conflicts)} claim(s) show material support AND contradiction "
                     f"from different sources (weakest consensus: claim “{(worst.explanation or '')[:160]}”). "
                     "Sources may be circular or the story may be genuinely contested — "
                     "manual review recommended."),
            metrics={"conflicting_claims": len(strong_conflicts),
                     "supporting": worst.supporting_count, "contradicting": worst.contradicting_count,
                     "verdict": worst.verdict},
            verification_job_id=job_id,
            dedupe_key=f"evidence_conflict:{job_id}")

    # 5) media signals — manipulation / AI-generation
    for media in db.query(MediaAnalysis).filter(MediaAnalysis.job_id == job_id).all():
        manip = media.manipulation_risk
        if manip is not None and float(manip) >= MEDIA_MANIPULATION_ALERT:
            sev = "HIGH" if float(manip) >= 0.8 else "MEDIUM"
            raised += _raise(
                db,
                kind="media_signal",
                severity=sev,
                title=f"Suspicious media signals (job {job_id})",
                message=(f"Media forensics rates manipulation risk at {float(manip):.2f} for "
                         f"“{media.file_name or media.media_type}”. Signals are heuristic — "
                         "see the media analysis section before drawing conclusions."),
                metrics={"manipulation_risk": float(manip), "media_type": media.media_type,
                         "detectors_run": json.loads(media.detectors_run) if media.detectors_run else []},
                verification_job_id=job_id,
                dedupe_key=f"media_manip:{job_id}:{media.id}")
        ai_conf = media.ai_signal_confidence
        signal = (media.ai_generation_signal or "").lower()
        if signal in ("high", "likely") and ai_conf is not None and float(ai_conf) >= AI_SIGNAL_ALERT_CONFIDENCE:
            raised += _raise(
                db,
                kind="media_signal",
                severity="MEDIUM",
                title=f"Possible AI-generated media (job {job_id})",
                message=(f"AI-generation detection reports signal “{media.ai_generation_signal}” "
                         f"(confidence {float(ai_conf):.0%}) for “{media.file_name or media.media_type}”. "
                         "Detector output is probabilistic and must be verified by a human."),
                metrics={"ai_generation_signal": media.ai_generation_signal,
                         "ai_signal_confidence": float(ai_conf)},
                verification_job_id=job_id,
                dedupe_key=f"media_ai:{job_id}:{media.id}")

    # high content risk from the verify-path risk engine
    summary = json.loads(job.result_summary) if job.result_summary else {}
    risk = (summary or {}).get("risk") or {}
    misinfo_val = risk.get("misinformation_risk")
    if isinstance(misinfo_val, (int, float)) and misinfo_val >= MISINFO_HIGH:
        raised += _raise(
            db,
            kind="misinfo_risk",
            severity=_severity_from_risk(float(misinfo_val)),
            title=f"High misinformation risk in verified content (job {job_id})",
            message=(f"Risk engine rates this content {misinfo_val:.2f} "
                     f"({job.input_kind}: {job.input_label}). Review the evidence report "
                     "before amplifying or suppressing."),
            metrics={"misinformation_risk": misinfo_val, "input_kind": job.input_kind},
            verification_job_id=job_id,
            dedupe_key=f"verify_misinfo:{job_id}")

    return raised


async def evaluate_verification_async(job_id: int) -> None:
    """Fire-and-forget wrapper used by the verification pipeline."""
    import asyncio

    from app.db.database import session_scope

    def _work() -> int:
        with session_scope() as db:
            return evaluate_verification_sync(db, job_id)

    try:
        await asyncio.to_thread(_work)
    except Exception:
        logger.exception("Verification alert evaluation failed for job %s", job_id)


# ------------------------------------------------------------------ query helpers
def select_latest(model, scope_col, scope_val):
    """Latest row per scope column (small helper keeps the engine readable)."""
    from sqlalchemy import select
    return (select(model).where(scope_col == scope_val)
            .order_by(model.timestamp.desc()).limit(1))


def select_previous(model, scope_col, scope_val, ts_col, before_ts, *, horizon=None):
    from sqlalchemy import select
    stmt = (select(model).where(scope_col == scope_val, ts_col < before_ts)
            .order_by(ts_col.desc()).limit(1))
    if horizon is not None and hasattr(model, "horizon_minutes"):
        stmt = stmt.where(model.horizon_minutes == horizon)
    return stmt

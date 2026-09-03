"""Prediction + intervention-score endpoints (spec #26, #53)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.repositories import (
    InterventionRepository,
    PostRepository,
    PredictionRepository,
    serialize_top_factors,
)
from app.schemas import (
    InterventionOut,
    PredictRequest,
    PredictionResponse,
    ScoreAllResponse,
)
from app.services.prediction_service import PredictionService

router = APIRouter(tags=["predictions"])


@router.get("/posts/{post_id}/prediction", response_model=PredictionResponse,
            summary="Full forecast + risk + intervention priority for a post",
            description="Runs (or reads the latest) scoring pipeline: XGBoost share forecast at "
                        "30/60/120m, misinformation risk, intervention priority with explanation. "
                        "Cold-start posts receive a transparent baseline forecast with lower confidence.")
def get_prediction(post_id: int, refresh: bool = Query(False, description="Recompute now"),
                         db: Session = Depends(get_db)) -> PredictionResponse:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    if refresh:
        return PredictionResponse(**PredictionService.score_post(db, post))
    stored = PredictionService.score_post(db, post, persist=False)
    return PredictionResponse(**stored)


@router.get("/posts/{post_id}/intervention-score", response_model=InterventionOut,
            summary="Latest stored intervention score",
            responses={404: {"description": "Post or score not found"}})
def get_intervention_score(post_id: int, db: Session = Depends(get_db)) -> InterventionOut:
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    score = InterventionRepository.latest_for_post(db, post_id)
    if not score:
        raise HTTPException(404, f"No intervention score computed yet for post {post_id}")
    return InterventionOut(
        post_id=post_id, timestamp=score.timestamp,
        spread_score=score.spread_score,
        misinformation_score=score.misinformation_score,
        intervention_priority=score.intervention_priority,
        priority_label=score.priority_label,
        explanation=score.explanation,
        top_factors=serialize_top_factors(score.top_factors),
    )


@router.post("/ml/predict", response_model=ScoreAllResponse, tags=["ml"],
             summary="Trigger fresh scoring for a post (or all posts when post_id omitted)",
             description="Recomputes features + predictions + intervention score. Used by the dashboard refresh and tests.")
def run_prediction(request: PredictRequest, db: Session = Depends(get_db)) -> ScoreAllResponse:
    post = PostRepository.get_by_id(db, request.post_id)
    if not post:
        raise HTTPException(404, f"Post {request.post_id} not found")
    result = PredictionService.score_post(db, post, persist=request.persist)
    return ScoreAllResponse(scored=1, results=[PredictionResponse(**result)])


@router.get("/predictions/{post_id}", response_model=list, tags=["predictions"],
            summary="Prediction history for a post", description="Most recent stored predictions with model versions.")
def prediction_history(post_id: int, db: Session = Depends(get_db)):
    post = PostRepository.get_by_id(db, post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    rows = PredictionRepository.for_post(db, post_id)
    return [
        {
            "prediction_timestamp": p.prediction_timestamp,
            "horizon_minutes": p.horizon_minutes,
            "predicted_additional_shares": p.predicted_additional_shares,
            "predicted_total_shares": p.predicted_total_shares,
            "confidence": p.confidence,
            "prediction_type": p.prediction_type,
            "model_name": p.model_name,
            "model_version": p.model_version,
        } for p in rows
    ]

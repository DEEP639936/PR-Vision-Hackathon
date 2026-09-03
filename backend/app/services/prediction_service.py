"""Prediction service — orchestrates features → forecast → misinfo → priority.

This is the end-to-end scoring path for a single post:

    snapshots ─► feature vector (causal) ─► XGBoost forecast (or baseline)
                                          └► misinformation risk (model+heuristic)
                    └► spread risk ─► Intervention Priority Score + explanation

All results are persisted (predictions / misinformation_scores /
intervention_scores) with model versions for auditability.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Post
from app.db.repositories import (
    InterventionRepository,
    MisinformationRepository,
    MetricSnapshotRepository,
    PostRepository,
    PredictionRepository,
)
from app.ml.inference import ModelManager
from app.services.feature_service import FeatureService
from app.services.scoring_service import build_explanation, combine, spread_risk, top_factors

logger = get_logger("prvision.services.prediction")


class PredictionService:
    @staticmethod
    def score_post(db: Session, post: Post, *, persist: bool = True) -> dict[str, Any]:
        """Full scoring pipeline for a post. Returns the API payload (spec #53)."""
        manager = ModelManager.instance()
        snapshot_count = len(MetricSnapshotRepository.history(db, post.id))
        features = FeatureService.compute_and_persist(db, post)

        # 1) forecast (XGBoost or transparent baseline)
        horizon_outputs = manager.predict_additional_shares(features, snapshot_count=snapshot_count)

        # 2) misinformation risk
        risk_score, risk_label_value, risk_layer = manager.misinformation_risk(post.content)
        misinfo = {"risk_score": risk_score, "risk_label": risk_label_value, "layer": risk_layer}

        # 3) spread risk + intervention priority
        f60 = horizon_outputs.get(60) or next(iter(horizon_outputs.values()))
        spread = spread_risk(
            predicted_additional_60m=f60.get("predicted_additional_shares"),
            share_velocity=features.get("share_velocity"),
            unique_sharer_growth_rate=features.get("unique_sharer_growth_rate"),
        )
        priority_score, priority_label_value = combine(spread=spread, misinfo=risk_score)

        # 4) explainability from observed features only
        explanation_header, reasons = build_explanation(
            features, horizon_outputs, misinfo,
            priority_score=priority_score, priority_label_str=priority_label_value,
        )
        factors = top_factors(features, horizon_outputs, misinfo, spread)

        if persist:
            current_time = features.get("timestamp")
            primary = horizon_outputs.get(60)
            if primary:
                PredictionRepository.add(
                    db,
                    post_id=post.id,
                    prediction_timestamp=current_time,
                    horizon_minutes=60,
                    predicted_additional_shares=primary["predicted_additional_shares"],
                    predicted_total_shares=primary["predicted_total_shares"],
                    confidence=primary["confidence"],
                    prediction_type=primary["prediction_type"],
                    model_name=primary["model_name"],
                    model_version=primary["model_version"],
                )
            for horizon, output in horizon_outputs.items():
                if horizon == 60:
                    continue  # primary already stored
                PredictionRepository.add(
                    db,
                    post_id=post.id,
                    prediction_timestamp=current_time,
                    horizon_minutes=horizon,
                    predicted_additional_shares=output["predicted_additional_shares"],
                    predicted_total_shares=output["predicted_total_shares"],
                    confidence=output["confidence"],
                    prediction_type=output["prediction_type"],
                    model_name=output["model_name"],
                    model_version=output["model_version"],
                )
            MisinformationRepository.add(
                db,
                post_id=post.id,
                timestamp=current_time,
                risk_score=risk_score,
                risk_label=risk_label_value,
                model_version=manager.loaded_misinfo_version or "heuristic-only",
            )
            InterventionRepository.add(
                db,
                post_id=post.id,
                timestamp=current_time,
                spread_score=spread,
                misinformation_score=risk_score,
                intervention_priority=priority_score,
                priority_label=priority_label_value,
                explanation=" | ".join(reasons),
                top_factors=json.dumps(factors),
                model_version=manager.loaded_misinfo_version or "v1-rules",
            )

        return {
            "post_id": str(post.id),
            "external_post_id": post.external_post_id,
            "platform": post.platform,
            "horizons": {
                str(h): {
                    "horizon_minutes": h,
                    "predicted_additional_shares": o["predicted_additional_shares"],
                    "predicted_total_shares": o["predicted_total_shares"],
                    "confidence": o["confidence"],
                    "prediction_type": o["prediction_type"],
                    **({"reason": o["reason"]} if o.get("reason") else {}),
                    "model": o["model_name"],
                    "model_version": o["model_version"],
                } for h, o in horizon_outputs.items()
            },
            "current_shares": features.get("current_shares"),
            "share_velocity": features.get("share_velocity"),
            "share_acceleration": features.get("share_acceleration"),
            "engagement_velocity": features.get("engagement_velocity"),
            "unique_sharer_growth_rate": features.get("unique_sharer_growth_rate"),
            "propagation_breadth": features.get("propagation_breadth"),
            "spread_risk": spread,
            "misinformation_risk": risk_score,
            "misinformation_risk_label": risk_label_value,
            "misinformation_model_layer": risk_layer,
            "intervention_priority": priority_score,
            "priority_label": priority_label_value,
            "explanation": reasons,
            "explanation_header": explanation_header,
            "top_factors": factors,
        }

    @staticmethod
    def score_all_active(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
        """Score every monitored post (used after ingestion cycles / demo gen)."""
        posts, _total = PostRepository.list_posts(db, limit=limit, offset=0)
        results = []
        for post in posts:
            try:
                results.append(PredictionService.score_post(db, post))
            except Exception:
                logger.exception("Scoring failed for post %s — skipped", post.id)
        return results

    @staticmethod
    def forecast_payload(db: Session, post: Post) -> dict[str, Any]:
        """Convenience payload combining historical series + forecast points."""
        manager = ModelManager.instance()
        features = FeatureService.compute_latest(db, post)
        snapshot_count = len(MetricSnapshotRepository.history(db, post.id))
        horizon_outputs = manager.predict_additional_shares(features, snapshot_count=snapshot_count)
        current_shares = features.get("current_shares") or 0.0
        forecast_points = []
        for horizon, output in sorted(horizon_outputs.items()):
            forecast_points.append({
                "horizon_minutes": horizon,
                "predicted_additional_shares": output["predicted_additional_shares"],
                "predicted_total_shares": output["predicted_total_shares"],
                "confidence": output["confidence"],
                "prediction_type": output["prediction_type"],
            })
        return {
            "post_id": str(post.id),
            "current_shares": current_shares,
            "share_velocity": features.get("share_velocity"),
            "forecast": forecast_points,
        }

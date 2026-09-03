"""ML training/status endpoints (spec #26, #46)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.ml.inference import ModelManager
from app.ml.runtime import ML_RUNTIME
from app.schemas import ModelStatusResponse, TrainResponse

router = APIRouter(prefix="/ml", tags=["ml"])


@router.post("/train", response_model=TrainResponse, summary="Train forecasting + misinfo models",
             description="Trains XGBoost models for every configured horizon (chronological split, "
                         "baseline comparison) plus the misinformation-risk model. Artifacts are versioned "
                         "in the model registry; models are hot-reloaded after training.")
async def train(db: Session = Depends(get_db)) -> TrainResponse:
    import asyncio

    if not ML_RUNTIME["available"]:
        # Heavy numeric libs are absent (light publish runtime) — re-training
        # genuinely requires them. The trained models still SERVE via the
        # portable exported-weights engine (see app/ml/portable.py); this
        # endpoint only fails the (re-)training request, honestly.
        raise HTTPException(
            status_code=503,
            detail=(ML_RUNTIME["reason"] or "") +
                   " Trained models keep serving via the portable exported-weights "
                   "engine; (re-)training itself needs the full runtime (Docker).")

    # Imported lazily: app.ml.training pulls numpy/scikit-learn/xgboost at
    # module level and must not run at app-boot import time.
    from app.ml.training import train_forecast_model, train_misinformation_model

    horizons_result: dict = {}
    for horizon in settings.horizons:
        # XGBoost training is CPU-heavy — run off the event loop.
        horizons_result[str(horizon)] = await asyncio.to_thread(
            train_forecast_model, db, horizon, models_dir=settings.models_dir)
    misinfo_result = await asyncio.to_thread(
        train_misinformation_model, models_dir=settings.models_dir)

    # Refresh the portable exported-weights bundle so slim runtimes serve the
    # freshly trained models too (best-effort — native paths are unaffected).
    try:
        import importlib.util as _ilu
        from pathlib import Path as _Path
        _spec = _ilu.spec_from_file_location(
            "export_portable_models",
            _Path(__file__).resolve().parents[4] / "scripts" / "export_portable_models.py")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        export_exit = _mod.main()
    except Exception as exc:
        export_exit = f"skipped: {exc.__class__.__name__}"

    reload = ModelManager.instance().load_models()
    overall = "trained" if any(v.get("status") == "trained" for v in horizons_result.values()) else "skipped"
    return TrainResponse(
        status=overall,
        horizons=horizons_result,
        misinformation=misinfo_result,
        detail=f"reloaded models: {reload}; portable export: {export_exit}",
    )


@router.get("/status", response_model=ModelStatusResponse, summary="Model status (spec #46)",
            description="Per-model name, version, training date, dataset size, metrics, loaded state. Reflects the real registry.")
def model_status() -> ModelStatusResponse:
    status = ModelManager.instance().status()
    return ModelStatusResponse(
        forecast=status["forecast"],
        misinformation=status["misinformation"],
        feature_count=status["feature_count"],
        runtime=status.get("runtime"),
        engine=status.get("engine"),
        engine_note=status.get("engine_note"),
    )

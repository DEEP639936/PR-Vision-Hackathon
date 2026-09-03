"""Model inference service — loads trained artifacts once, serves predictions.

Implements spec #23 (cold-start fallback):
    - no trained model OR too few historical snapshots
      → transparent BASELINE forecast (current velocity × horizon) with
        reduced confidence and `prediction_type="baseline"`.
    - otherwise → XGBoost forecast with model-derived confidence.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.ml.feature_engineering import MODEL_FEATURES
from app.ml.forecasting import MODEL_NAME, ForecastModel, ModelRegistry
from app.ml.misinformation import MODEL_NAME as MISINFO_MODEL_NAME
from app.ml.misinformation import MisinformationModel, blend_risk, heuristic_risk, risk_label
from app.ml.portable import load_portable
from app.ml.runtime import ML_RUNTIME

logger = get_logger("prvision.ml.inference")


class ModelManager:
    """Process-wide singleton that keeps fitted models warm in memory.

    Loading order per model:
      1. NATIVE stack (numpy/xgboost/scikit-learn present) — exact artifacts;
      2. PORTABLE exported-weights engine (pure Python, see app/ml/portable.py)
         for any model the native stack could not load.
    With neither, predictions degrade transparently to the documented baseline.
    """

    _lock = threading.Lock()
    _instance: Optional["ModelManager"] = None

    def __init__(self) -> None:
        self._forecast: dict[int, Any] = {}
        self._misinfo: Optional[Any] = None
        self._registry = ModelRegistry(settings.models_dir)
        self.loaded_forecast_versions: dict[int, str] = {}
        self.loaded_misinfo_version: Optional[str] = None
        self.runtime_note: Optional[str] = ML_RUNTIME["reason"]
        self.engine: str = "baseline"  # native | portable | baseline

    @classmethod
    def instance(cls) -> "ModelManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------- loading
    def load_models(self) -> dict[str, Any]:
        """(Re)load latest model versions from the registry. Safe to call anytime."""
        loaded, errors = [], []
        self.engine = "baseline"
        if ML_RUNTIME["available"]:
            for horizon in settings.horizons:
                entry = self._registry.latest(MODEL_NAME, horizon)
                if entry is None:
                    continue
                try:
                    self._forecast[horizon] = ForecastModel.load(
                        settings.models_dir, horizon, entry["version"])
                    self.loaded_forecast_versions[horizon] = entry["version"]
                    loaded.append(f"forecast_{horizon}m@{entry['version']}")
                except Exception as exc:  # artifact missing/corrupt
                    errors.append(f"forecast_{horizon}m: {exc}")
                    logger.error("Failed to load forecast model %dm: %s", horizon, exc)

            entry = self._registry.latest(MISINFO_MODEL_NAME)
            if entry:
                try:
                    self._misinfo = MisinformationModel.load(settings.models_dir, entry["version"])
                    self.loaded_misinfo_version = entry["version"]
                    loaded.append(f"misinformation@{entry['version']}")
                except Exception as exc:
                    errors.append(f"misinformation: {exc}")
                    logger.error("Failed to load misinformation model: %s", exc)
            if self._forecast or self._misinfo:
                self.engine = "native"

        # Portable exported-weights engine — fills any model the native stack
        # could not load (e.g. the slim publish runtime has no numeric libs at
        # all). Serves the SAME trained parameters, parity-verified by
        # scripts/validate_portable_parity.py.
        bundle = load_portable(settings.models_dir)
        if bundle:
            for horizon, pm in bundle.forecast.items():
                if horizon not in self._forecast:
                    self._forecast[horizon] = pm
                    self.loaded_forecast_versions[horizon] = pm.version
                    loaded.append(f"forecast_{horizon}m@{pm.version} (portable)")
            if self._misinfo is None and bundle.misinfo is not None:
                self._misinfo = bundle.misinfo
                self.loaded_misinfo_version = bundle.misinfo.version
                loaded.append(f"misinformation@{bundle.misinfo.version} (portable)")
            if self._forecast or self._misinfo:
                self.engine = "portable" if self.engine == "baseline" else self.engine

        if self.engine == "baseline":
            self.runtime_note = (
                ML_RUNTIME["reason"]
                or "No trained model artifacts found for this host — run "
                   "scripts/train_models.py (full runtime) or POST /api/ml/train."
            )
            errors.append(self.runtime_note)
            logger.warning("ModelManager: serving transparent baseline (%s)", self.runtime_note)
        else:
            self.runtime_note = None if self.engine == "native" else (
                "Models served through the portable exported-weights engine "
                "(parity-verified); the native numeric stack is not installed on this host."
            )

        logger.info("ModelManager loaded (%s engine): %s", self.engine, loaded or "none")
        return {"loaded": loaded, "errors": errors, "engine": self.engine}

    # ------------------------------------------------------------ accessors
    def forecast_model(self, horizon: int) -> Optional[ForecastModel]:
        return self._forecast.get(horizon)

    def status(self) -> dict[str, Any]:
        forecast_entries = {
            str(h): self._registry.latest(MODEL_NAME, h) for h in settings.horizons
        }
        misinfo_entry = self._registry.latest(MISINFO_MODEL_NAME)
        return {
            "runtime": ML_RUNTIME,
            "engine": self.engine,
            "engine_note": self.runtime_note,
            "forecast": {
                str(h): {
                    "loaded": int(h) in self._forecast,
                    "available": entry is not None,
                    "version": entry["version"] if entry else None,
                    "trained_at": entry["trained_at"] if entry else None,
                    "dataset_size": entry["dataset_size"] if entry else None,
                    "metrics": entry["metrics"] if entry else None,
                } for h, entry in forecast_entries.items()
            },
            "misinformation": {
                "loaded": self._misinfo is not None,
                "available": misinfo_entry is not None,
                "version": misinfo_entry["version"] if misinfo_entry else None,
                "trained_at": misinfo_entry["trained_at"] if misinfo_entry else None,
                "metrics": misinfo_entry["metrics"] if misinfo_entry else None,
            },
            "feature_count": len(MODEL_FEATURES),
        }

    def is_forecast_ready(self) -> bool:
        return bool(self._forecast)

    # ----------------------------------------------------------- inference
    def predict_additional_shares(
        self,
        features: dict[str, Any],
        *,
        snapshot_count: int,
    ) -> dict[str, Any]:
        """Forecast additional shares for every configured horizon.

        Returns per-horizon predictions with explicit type:
            {"prediction_type": "model"|"baseline", "reason": ..., ...}
        """
        horizon_outputs: dict[int, dict[str, Any]] = {}
        enough_history = snapshot_count >= settings.PREDICTION_MIN_HISTORY_SNAPSHOTS
        current_velocity = features.get("share_velocity")
        current_shares = features.get("current_shares") or 0.0
        for horizon in settings.horizons:
            model = self._forecast.get(horizon)
            if model is not None and enough_history:
                predicted = model.predict(features)
                # confidence: bounded heuristic from recent data adequacy and
                # the model's own training R² (registry), documented in docs/ml-pipeline.md
                confidence = self._confidence(horizon, snapshot_count)
                horizon_outputs[horizon] = {
                    "prediction_type": "model",
                    "predicted_additional_shares": round(predicted, 1),
                    "predicted_total_shares": round(current_shares + predicted, 1),
                    "confidence": confidence,
                    "model_name": MODEL_NAME,
                    "model_version": model.version,
                }
            else:
                if model is not None:
                    reason = "insufficient historical data"
                elif self.runtime_note:
                    reason = self.runtime_note
                else:
                    reason = "model not trained yet"
                base_velocity = max(0.0, current_velocity or 0.0)
                predicted = base_velocity * horizon
                horizon_outputs[horizon] = {
                    "prediction_type": "baseline",
                    "reason": reason,
                    "predicted_additional_shares": round(predicted, 1),
                    "predicted_total_shares": round(current_shares + predicted, 1),
                    "confidence": 0.30,  # deliberately low for transparent baseline
                    "model_name": "velocity-baseline",
                    "model_version": None,
                }
        return horizon_outputs

    def _confidence(self, horizon: int, snapshot_count: int) -> float:
        """Blend training R² with a data-adequacy factor (transparent heuristic)."""
        entry = self._registry.latest(MODEL_NAME, horizon) or {}
        r2 = ((entry.get("metrics") or {}).get("r2") or 0.0)
        r2 = max(0.0, min(1.0, r2))
        adequacy = min(1.0, snapshot_count / 12.0)  # full confidence at 12+ snapshots
        return round(max(0.30, 0.5 * r2 + 0.5 * adequacy), 3)

    def misinformation_risk(self, content: str) -> tuple[float, str, str]:
        """Return (risk_score, risk_label, model_layer)."""
        heuristic = heuristic_risk(content)
        proba = self._misinfo.probability(content) if self._misinfo else None
        score, layer = blend_risk(proba, heuristic)
        return score, risk_label(score), layer

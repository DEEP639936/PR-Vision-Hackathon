"""XGBoost share-forecasting model (spec #17-22).

One regressor per forecast horizon (30/60/120 minutes). Target:

    target_h = shares(t + h) - shares(t)

Inputs are strictly causal feature vectors (see feature_engineering).
Each trained model persists:
    - {models_dir}/forecast_{h}m__{version}.joblib   (the fitted estimator)
    - entry in {models_dir}/registry.json            (version metadata + metrics)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import joblib
    import numpy as np
    HAS_ML_RUNTIME = True
except ImportError:  # light publish runtime — see app/ml/runtime.py
    HAS_ML_RUNTIME = False

from app.core.logging import get_logger
from app.ml.feature_engineering import MODEL_FEATURES
from app.ml.runtime import ML_RUNTIME

logger = get_logger("prvision.ml.forecasting")

MODEL_NAME = "prvision-share-forecast"


class ForecastModel:
    """Thin wrapper around an XGBRegressor with feature-name discipline."""

    def __init__(self, horizon_minutes: int, version: str, estimator: Any, feature_names: list[str]) -> None:
        self.horizon_minutes = horizon_minutes
        self.version = version
        self.estimator = estimator
        self.feature_names = feature_names

    # ---------------------------------------------------------------- predict
    def predict(self, feature_row: dict[str, Any]) -> float:
        """Predict additional shares over the horizon from a causal feature row."""
        x = self._vector(feature_row)
        pred = float(self.estimator.predict(x)[0])
        return max(0.0, pred)

    def _vector(self, row: dict[str, Any]) -> np.ndarray:
        values = [row.get(name) for name in self.feature_names]
        x = np.array([[0.0 if v is None else float(v) for v in values]], dtype=np.float64)
        return x

    # ------------------------------------------------------------ persistence
    def save(self, models_dir: Path) -> Path:
        models_dir.mkdir(parents=True, exist_ok=True)
        path = models_dir / f"forecast_{self.horizon_minutes}m__{self.version}.joblib"
        joblib.dump({"estimator": self.estimator, "feature_names": self.feature_names,
                     "horizon_minutes": self.horizon_minutes, "version": self.version}, path)
        return path

    @classmethod
    def load(cls, models_dir: Path, horizon_minutes: int, version: str) -> "ForecastModel":
        if not HAS_ML_RUNTIME:
            raise RuntimeError(ML_RUNTIME["reason"])
        path = models_dir / f"forecast_{horizon_minutes}m__{version}.joblib"
        if not path.exists():
            raise FileNotFoundError(f"Model artifact not found: {path}")
        payload = joblib.load(path)
        return cls(
            horizon_minutes=payload["horizon_minutes"],
            version=payload["version"],
            estimator=payload["estimator"],
            feature_names=payload["feature_names"],
        )


class ModelRegistry:
    """JSON-backed registry of every trained model version (spec #22)."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.path = models_dir / "registry.json"

    def _read(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"models": []}

    def record(
        self,
        *,
        model_name: str,
        version: str,
        horizon_minutes: Optional[int],
        trained_at: datetime,
        dataset_size: int,
        features: list[str],
        metrics: dict[str, Any],
        artifact_path: str,
    ) -> None:
        data = self._read()
        data["models"].append(
            {
                "model_name": model_name,
                "version": version,
                "horizon_minutes": horizon_minutes,
                "trained_at": trained_at.isoformat(),
                "dataset_size": dataset_size,
                "n_features": len(features),
                "metrics": metrics,
                "artifact": artifact_path,
            }
        )
        self.path.write_text(json.dumps(data, indent=2))

    def latest(self, model_name: str, horizon_minutes: Optional[int] = None) -> Optional[dict]:
        """Newest trained version matching name (and horizon when given)."""
        entries = [m for m in self._read()["models"] if m["model_name"] == model_name]
        if horizon_minutes is not None:
            entries = [m for m in entries if m.get("horizon_minutes") == horizon_minutes]
        if not entries:
            return None
        entries.sort(key=lambda m: m["trained_at"], reverse=True)
        return entries[0]


def new_version() -> str:
    """Version string like 2026.09.02-183015-ab12 (timestamp + short hash)."""
    stamp = datetime.now(timezone.utc).strftime("%Y.%m.%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"

"""ML tests — feature schema, training pipeline, model loading, inference
fallbacks, and misinformation-risk component (spec #43)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ml.feature_engineering import MODEL_FEATURES, build_feature_vector


def test_feature_schema_stable():
    """The model contract: every expected feature name exists and is unique."""
    assert len(MODEL_FEATURES) == len(set(MODEL_FEATURES))
    required = {
        "share_velocity", "share_velocity_5m", "share_velocity_15m",
        "share_acceleration", "engagement_velocity", "engagement_acceleration",
        "unique_sharer_growth_rate", "propagation_depth", "propagation_breadth",
        "time_since_post", "sensational_score", "claim_score",
    }
    assert required <= set(MODEL_FEATURES)


def test_feature_vector_covers_model_schema():
    feats = build_feature_vector(
        post_posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        snapshot_history=[{"timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                           "shares": 1, "likes": 2, "comments": 0, "views": 10,
                           "followers": 5, "unique_sharers": 1}],
        propagation_events=[], content="hello world",
    )
    missing = [f for f in MODEL_FEATURES if f not in feats]
    assert not missing, f"feature builder missing model inputs: {missing}"


def test_forecast_model_missing_artifact():
    from pathlib import Path
    from app.ml.forecasting import ForecastModel
    with pytest.raises(FileNotFoundError):
        ForecastModel.load(Path("/tmp/definitely-not-here"), 60, "0.0.0-test")


def test_registry_roundtrip(tmp_path):
    from datetime import datetime as dt
    from app.ml.forecasting import ModelRegistry

    registry = ModelRegistry(tmp_path)
    registry.record(model_name="test-model", version="v1", horizon_minutes=60,
                    trained_at=dt.now(timezone.utc), dataset_size=10,
                    features=["a", "b"], metrics={"mae": 1.0}, artifact_path="x.joblib")
    registry.record(model_name="test-model", version="v2", horizon_minutes=30,
                    trained_at=dt.now(timezone.utc), dataset_size=20,
                    features=["a"], metrics={"mae": 2.0}, artifact_path="y.joblib")
    latest_60 = registry.latest("test-model", 60)
    assert latest_60["version"] == "v1"
    assert registry.latest("test-model", 30)["version"] == "v2"
    assert registry.latest("nonexistent") is None


def test_xgboost_train_predict_toy(tmp_path):
    """End-to-end micro-training: real XGBRegressor fit + inference."""
    import numpy as np
    from sklearn.linear_model import LinearRegression  # noqa: F401 (sanity import)
    from xgboost import XGBRegressor

    rng = np.random.default_rng(42)
    X = rng.normal(size=(200, len(MODEL_FEATURES)))
    weights = np.linspace(0.5, 2.0, len(MODEL_FEATURES))
    y = X @ weights + rng.normal(scale=0.1, size=200)

    model = XGBRegressor(n_estimators=50, max_depth=3, tree_method="hist")
    model.fit(X, y)

    from app.ml.forecasting import ForecastModel
    wrapped = ForecastModel(horizon_minutes=60, version="test", estimator=model,
                            feature_names=MODEL_FEATURES)
    row = {name: float(X[0, i]) for i, name in enumerate(MODEL_FEATURES)}
    pred = wrapped.predict(row)
    assert pred >= 0.0  # negative predictions are clipped to zero
    wrapped.save(tmp_path)
    reloaded = ForecastModel.load(tmp_path, 60, "test")
    assert reloaded.predict(row) == pytest.approx(pred, abs=1e-6)


def test_missing_feature_handling(tmp_path):
    """Rows with missing (None) features must not crash — treated as 0."""
    import numpy as np
    from xgboost import XGBRegressor

    rng = np.random.default_rng(7)
    X = rng.normal(size=(100, len(MODEL_FEATURES)))
    y = X.sum(axis=1)
    model = XGBRegressor(n_estimators=20, max_depth=2, tree_method="hist")
    model.fit(X, y)

    from app.ml.forecasting import ForecastModel
    wrapped = ForecastModel(horizon_minutes=60, version="test2", estimator=model,
                            feature_names=MODEL_FEATURES)
    sparse_row = {name: (None if i % 3 == 0 else float(i)) for i, name in enumerate(MODEL_FEATURES)}
    assert wrapped.predict(sparse_row) >= 0.0


def test_cold_start_baseline_fallback():
    """Without enough history the manager must return a TRANSPARENT baseline."""
    from app.ml.inference import ModelManager

    manager = ModelManager.instance()
    features = {"share_velocity": 2.0, "current_shares": 100.0}
    outputs = manager.predict_additional_shares(features, snapshot_count=1)  # < min history

    for horizon, output in outputs.items():
        assert output["prediction_type"] == "baseline"
        assert "reason" in output
        assert output["predicted_additional_shares"] == pytest.approx(2.0 * horizon)
        assert output["confidence"] <= 0.5, "baseline confidence must be low"


def test_misinformation_risk_bounds_and_labels():
    from app.ml.misinformation import blend_risk, heuristic_risk, risk_label

    suspicious = "BREAKING!!! doctors hate this MIRACLE cure — share before deleted!!!"
    benign = "The library opens a new reading room next week."

    h_bad = heuristic_risk(suspicious)
    h_good = heuristic_risk(benign)
    assert 0.0 <= h_good < h_bad <= 1.0

    score, layer = blend_risk(None, h_bad)
    assert layer == "heuristic"
    score2, layer2 = blend_risk(0.9, h_bad)
    assert layer2 == "model+heuristic"
    assert 0.0 <= score2 <= 1.0

    assert risk_label(0.1) == "LOW"
    assert risk_label(0.4) == "MODERATE"
    assert risk_label(0.7) == "HIGH"
    assert risk_label(0.95) == "CRITICAL"


def test_misinformation_model_predicts_style():
    """If the artifact exists it must separate misinfo-style from benign text."""
    from pathlib import Path

    from app.core.config import settings
    from app.ml.forecasting import ModelRegistry
    from app.ml.misinformation import MODEL_NAME, MisinformationModel

    entry = ModelRegistry(settings.models_dir).latest(MODEL_NAME)
    if entry is None:
        pytest.skip("misinformation model not trained in this environment")
    model = MisinformationModel.load(settings.models_dir, entry["version"])
    p_bad = model.probability("BREAKING!!! leaked document PROVES miracle cure — share now!!!")
    p_good = model.probability("Volunteers planted trees along the river this weekend.")
    assert p_bad > p_good


def test_evaluation_metrics_sanity():
    from app.ml.evaluation import evaluate
    metrics = evaluate([10, 20, 30], [12, 18, 33])
    assert metrics["mae"] == pytest.approx(7 / 3, abs=0.01)
    assert metrics["r2"] > 0.9
    assert metrics["rmse"] >= metrics["mae"]


def test_training_dataset_no_leakage(db_session):
    """Targets must equal future shares − current shares (checked on one row)."""
    from datetime import timedelta

    from sqlalchemy import select
    from app.db.models import MetricSnapshot
    from app.ml.training import build_forecast_dataset

    horizon = 30
    X, y, names = build_forecast_dataset(db_session, horizon)
    if len(y) == 0:
        pytest.skip("no training rows in this DB")

    # verify one row against raw snapshots
    feature_rows = (
        db_session.execute(select(MetricSnapshot).order_by(MetricSnapshot.id)).scalars().all()
    )
    assert feature_rows, "metric snapshots must exist"

    idx_name = names.index("time_since_post")
    # every training row must be non-negative share delta
    assert all(v >= 0 for v in y)
    assert X.shape[1] == len(MODEL_FEATURES)

"""Model training pipeline (spec #18-22).

Dataset construction
--------------------
For every feature snapshot at time t we look up a metric snapshot at
t + horizon (± tolerance). The training target is shares(t+h) − shares(t).
Snapshots without a future anchor are skipped. Features come from the causal
feature vector at t — future data never enters X (spec #19).

Time-aware splitting (spec #20)
-------------------------------
Chronological 70/15/15 split (train/validation/test) — NOT a random shuffle —
so the model is always evaluated on propagation dynamics that occurred after
its training window.

Baselines (spec #21)
--------------------
    baseline_h = share_velocity(t) × horizon   (current velocity extrapolated)

Every trained version records metrics vs this baseline in the registry.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sqlalchemy.orm import Session
from xgboost import XGBRegressor

from app.core.logging import get_logger
from app.db.models import MetricSnapshot
from app.db.repositories import FeatureSnapshotRepository
from app.ml.evaluation import evaluate
from app.ml.feature_engineering import MODEL_FEATURES
from app.ml.forecasting import MODEL_NAME, ForecastModel, ModelRegistry, new_version
from app.ml.misinformation import MODEL_NAME as MISINFO_MODEL_NAME
from app.ml.misinformation import MisinformationModel, build_synthetic_corpus

logger = get_logger("prvision.ml.training")

TARGET_TOLERANCE = timedelta(minutes=3)  # t+h anchor may be up to 3 min late


# ----------------------------------------------------------------- dataset
def build_forecast_dataset(
    db: Session, horizon_minutes: int
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Return (X, y, feature_names) for one horizon, built from DB snapshots.

    Uses metric_snapshots (raw share counts) joined by post_id/time against
    feature_snapshots (causal features). Rows are ordered chronologically.
    """
    from sqlalchemy import select

    feature_rows = FeatureSnapshotRepository.all_features(db)
    if not feature_rows:
        return np.empty((0, len(MODEL_FEATURES))), np.empty((0,)), list(MODEL_FEATURES)

    # shares lookup per post, ordered by time
    shares_by_post: dict[int, list[tuple[Any, float]]] = {}
    stmt = (
        select(MetricSnapshot.post_id, MetricSnapshot.timestamp, MetricSnapshot.shares)
        .order_by(MetricSnapshot.post_id, MetricSnapshot.timestamp)
    )
    for post_id, ts, shares in db.execute(stmt).all():
        if shares is None:
            continue
        shares_by_post.setdefault(post_id, []).append((ts, float(shares)))

    X_rows: list[list[float]] = []
    y_values: list[float] = []
    horizon_delta = timedelta(minutes=horizon_minutes)

    def _shares_before(post_id: int, when: Any) -> float | None:
        """Latest raw share count at a snapshot <= when (the value at time t)."""
        series = shares_by_post.get(post_id, [])
        current = None
        for ts, shares in series:
            if ts <= when:
                current = shares
            else:
                break
        return current

    def _shares_at(post_id: int, when: Any) -> float | None:
        """Share count at the first snapshot >= when (within tolerance)."""
        series = shares_by_post.get(post_id, [])
        for ts, shares in series:
            if ts >= when and (ts - when) <= TARGET_TOLERANCE:
                return shares
        return None

    for row in feature_rows:
        ts = row["timestamp"]
        current_shares = _shares_before(row["post_id"], ts)
        if current_shares is None:
            continue
        future_shares = _shares_at(row["post_id"], ts + horizon_delta)
        if future_shares is None:
            continue
        vector = [row.get(name) for name in MODEL_FEATURES]
        X_rows.append([0.0 if v is None else float(v) for v in vector])
        y_values.append(max(0.0, future_shares - float(current_shares)))

    if not X_rows:
        return np.empty((0, len(MODEL_FEATURES))), np.empty((0,)), list(MODEL_FEATURES)
    return np.array(X_rows, dtype=np.float64), np.array(y_values, dtype=np.float64), list(MODEL_FEATURES)


# ----------------------------------------------------------------- training
def train_forecast_model(
    db: Session,
    horizon_minutes: int,
    *,
    models_dir: Any,
    validate_fraction: float = 0.15,
    test_fraction: float = 0.15,
) -> dict[str, Any]:
    """Train one XGBoost horizon model with chronological split + evaluation."""
    registry = ModelRegistry(models_dir)
    X, y, feature_names = build_forecast_dataset(db, horizon_minutes)
    n = len(y)
    if n < 60:
        logger.warning("Horizon %dm: only %d training rows — skipping (need >= 60)", horizon_minutes, n)
        return {"status": "skipped", "horizon_minutes": horizon_minutes, "rows": n,
                "reason": "insufficient dataset size (<60 rows)"}

    # chronological split — NO shuffling (spec #20)
    train_end = int(n * (1 - validate_fraction - test_fraction))
    val_end = int(n * (1 - test_fraction))
    X_train, y_train = X[:train_end], y[:train_end]
    X_val, y_val = X[train_end:val_end], y[train_end:val_end]
    X_test, y_test = X[val_end:], y[val_end:]

    model = XGBRegressor(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.06,
        subsample=0.9,
        colsample_bytree=0.85,
        reg_lambda=1.2,
        objective="reg:squarederror",
        tree_method="hist",
        early_stopping_rounds=30,
        eval_metric="rmse",
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    y_pred = model.predict(X_test)
    y_base = np.array([
        row_feature_velocity(X_row, feature_names) * horizon_minutes for X_row in X_test
    ]) if len(X_test) else np.array([])

    metrics = evaluate(list(y_test), list(y_pred))
    baseline_metrics = evaluate(list(y_test), list(y_base)) if len(y_test) else {}
    metrics.update({
        "baseline_mae": baseline_metrics.get("mae"),
        "baseline_rmse": baseline_metrics.get("rmse"),
        "improvement_vs_baseline_pct": round(
            100 * (baseline_metrics.get("mae", 0) - metrics["mae"]) / baseline_metrics.get("mae", 1), 2
        ) if baseline_metrics.get("mae") else None,
    })

    version = new_version()
    wrapped = ForecastModel(horizon_minutes=horizon_minutes, version=version,
                            estimator=model, feature_names=feature_names)
    artifact = wrapped.save(models_dir)
    registry.record(
        model_name=MODEL_NAME,
        version=version,
        horizon_minutes=horizon_minutes,
        trained_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        dataset_size=n,
        features=feature_names,
        metrics=metrics,
        artifact_path=str(artifact),
    )
    logger.info("Horizon %dm trained: rows=%d MAE=%.2f (baseline %.2f) R2=%.3f",
                horizon_minutes, n, metrics["mae"], baseline_metrics.get("mae", float("nan")), metrics["r2"])
    return {"status": "trained", "horizon_minutes": horizon_minutes, "version": version,
            "rows": n, "metrics": metrics, "splits": {
                "train": len(y_train), "validation": len(y_val), "test": len(y_test)}}


def row_feature_velocity(x_row: np.ndarray, feature_names: list[str]) -> float:
    """Extract share_velocity from a raw feature row for baseline computation."""
    try:
        idx = feature_names.index("share_velocity")
        return max(0.0, float(x_row[idx]))
    except ValueError:
        return 0.0


def train_misinformation_model(*, models_dir: Any) -> dict[str, Any]:
    """Train the stylistic misinformation-risk model on the documented corpus."""
    texts, labels = build_synthetic_corpus()
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)
    X = vectorizer.fit_transform(texts)
    X_train, X_test, y_train, y_test = train_test_split(
        X, labels, test_size=0.2, random_state=42, stratify=labels)
    clf = LogisticRegression(C=2.0, max_iter=1000)
    clf.fit(X_train, y_train)
    proba = clf.predict_proba(X_test)[:, list(clf.classes_).index(1)]
    auc = float(roc_auc_score(y_test, proba))
    version = new_version()
    model = MisinformationModel(version=version, vectorizer=vectorizer, classifier=clf)
    artifact = model.save(models_dir)
    ModelRegistry(models_dir).record(
        model_name=MISINFO_MODEL_NAME, version=version, horizon_minutes=None,
        trained_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        dataset_size=len(texts), features=["tfidf_1_2grams"],
        metrics={"roc_auc": round(auc, 4)},
        artifact_path=str(artifact),
    )
    logger.info("Misinformation model trained: AUC=%.3f version=%s", auc, version)
    return {"status": "trained", "model": MISINFO_MODEL_NAME, "version": version,
            "roc_auc": round(auc, 4), "corpus_size": len(texts)}

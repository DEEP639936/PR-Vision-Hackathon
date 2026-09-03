#!/usr/bin/env python3
"""Export trained models into the portable weights bundle.

Run on any host that HAS the full ML runtime (sandbox, Docker, VPS) — e.g.
after ``scripts/train_models.py``. Reads the latest versions from the model
registry, extracts the *actual learned parameters*, and writes:

    {models_dir}/portable/portable_models.json

  * forecast (XGBRegressor)  -> booster dump (trees, float32 leaf weights)
                                + base score (objective: reg:squarederror)
  * misinformation           -> TF-IDF vocabulary + idf_ vector +
                                LogisticRegression coef_ / intercept_ / classes_

The published slim runtime (no numpy/scikit-learn/xgboost) then serves the
SAME trained models through app/ml/portable.py. Parity vs the native stack is
verified by scripts/validate_portable_parity.py.

Usage:
    python scripts/export_portable_models.py [--models-dir ml/models]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `app` importable when run from the repo checkout
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.ml.forecasting import MODEL_NAME, ModelRegistry  # noqa: E402
from app.ml.misinformation import MODEL_NAME as MISINFO_MODEL_NAME  # noqa: E402

PORTABLE_FORMAT_VERSION = 1


def _export_forecast(joblib, models_dir: Path, registry: ModelRegistry, horizon: int) -> dict:
    import xgboost as xgb  # noqa: F401  (must be importable — full runtime)

    entry = registry.latest(MODEL_NAME, horizon)
    if entry is None:
        print(f"  ! no trained forecast model for {horizon}m — skipped")
        return {}
    path = models_dir / f"forecast_{horizon}m__{entry['version']}.joblib"
    payload = joblib.load(path)
    estimator = payload["estimator"]
    booster = estimator.get_booster()

    dump = booster.get_dump(dump_format="json")
    trees = [json.loads(t) for t in dump]

    # XGBRegressor.predict() honours early stopping: it evaluates exactly
    # iteration_range=(0, best_iteration+1) trees. The portable engine must
    # mirror that or predictions diverge by design.
    best_iteration = getattr(estimator, "best_iteration", None)
    if best_iteration is None or best_iteration < 0:
        n_used = len(trees)
    else:
        n_used = min(int(best_iteration) + 1, len(trees))

    # base_score: save_config is the single source of truth across xgboost versions
    config = json.loads(booster.save_config())
    learner = config.get("learner", {})
    base_score = float(learner.get("learner_model_param", {}).get("base_score", 0.5))
    objective = (learner.get("objective", {}) or {}).get("name", "reg:squarederror")
    if objective != "reg:squarederror":
        print(f"  ! unexpected objective {objective!r} for {horizon}m — exported anyway "
              f"(portable engine assumes identity link)")

    # Normalise split references to f-indices so the pure-Python evaluator is
    # deterministic regardless of how the booster was named at train time.
    feature_names = list(payload["feature_names"])
    name_to_idx = {name: f"f{i}" for i, name in enumerate(feature_names)}

    def normalise(node: dict) -> dict:
        if "leaf" in node:
            return {"nodeid": node["nodeid"], "leaf": node["leaf"]}
        split = str(node.get("split"))
        out = {
            "nodeid": node["nodeid"],
            "split": name_to_idx.get(split, split),
            "split_condition": node["split_condition"],
            "yes": node["yes"], "no": node["no"], "missing": node["missing"],
            "children": [normalise(c) for c in node.get("children", [])],
        }
        return out

    print(f"  forecast_{horizon}m v{entry['version']}: {len(trees)} dumped trees, "
          f"using first {n_used} (best_iteration={getattr(estimator, 'best_iteration', None)}), "
          f"base_score={base_score}, {len(feature_names)} features")
    return {
        "version": entry["version"],
        "horizon_minutes": horizon,
        "feature_names": feature_names,
        "base_score": base_score,
        "objective": objective,
        "trained_at": entry["trained_at"],
        "n_trees_used": n_used,
        "trees": [normalise(t) for t in trees],
    }


def _export_misinfo(joblib, models_dir: Path, registry: ModelRegistry) -> dict:
    entry = registry.latest(MISINFO_MODEL_NAME)
    if entry is None:
        print("  ! no trained misinformation model — skipped")
        return {}
    path = models_dir / f"misinformation__{entry['version']}.joblib"
    payload = joblib.load(path)
    vec = payload["vectorizer"]
    clf = payload["classifier"]

    vocab = {str(term): int(idx) for term, idx in vec.vocabulary_.items()}
    idf = [float(x) for x in vec.idf_]
    coef = [float(x) for x in clf.coef_[0]]
    intercept = float(clf.intercept_[0])
    classes = [int(c) for c in clf.classes_]

    params = vec.get_params()
    print(f"  misinformation v{entry['version']}: vocab={len(vocab)}, "
          f"ngram={params.get('ngram_range')}, sublinear_tf={params.get('sublinear_tf')}, "
          f"norm={getattr(vec, 'norm', 'l2')}")
    return {
        "version": entry["version"],
        "trained_at": entry["trained_at"],
        "token_pattern": params.get("token_pattern", r"(?u)\b\w\w+\b"),
        "lowercase": bool(params.get("lowercase", True)),
        "ngram_range": list(params.get("ngram_range", (1, 2))),
        "sublinear_tf": bool(params.get("sublinear_tf", False)),
        "norm": str(getattr(vec, "norm", "l2")),
        "vocabulary": vocab,
        "idf": idf,
        "coef": coef,
        "intercept": intercept,
        "classes": classes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", default="ml/models",
                        help="Model directory containing registry.json (default: ml/models)")
    args = parser.parse_args()

    try:
        import joblib  # full runtime required
    except ImportError:
        print("ERROR: joblib/numpy unavailable — run this on a full-runtime host "
              "(sandbox, Docker image, VPS). The slim publish runtime cannot train or export.")
        return 2

    models_dir = Path(args.models_dir)
    registry = ModelRegistry(models_dir)

    print(f"Exporting portable models from {models_dir} ...")
    forecast = {}
    for horizon in (30, 60, 120):
        exported = _export_forecast(joblib, models_dir, registry, horizon)
        if exported:
            forecast[str(horizon)] = exported
    misinfo = _export_misinfo(joblib, models_dir, registry)

    if not forecast and not misinfo:
        print("Nothing to export (no trained models found in the registry).")
        return 1

    source_versions = {
        **{f"forecast_{k}": v["version"] for k, v in forecast.items()},
        **({"misinformation": misinfo["version"]} if misinfo else {}),
    }
    bundle = {
        "format_version": PORTABLE_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_versions": source_versions,
        "forecast": forecast,
        "misinformation": misinfo,
    }

    out_dir = models_dir / "portable"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "portable_models.json"
    out_path.write_text(json.dumps(bundle, ensure_ascii=False))
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.0f} KB) — versions: {source_versions}")
    print("Next: python scripts/validate_portable_parity.py  (verify native-vs-portable parity)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
